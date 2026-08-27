// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The two editors for the operator's OWN rules: reasons to remove, which add to the deletion
// score, and reasons to keep, which either protect outright or lean toward keeping.
//
// Both are one form: a field, a comparison, a value, a weight, over a vocabulary the server
// publishes. Most of this file is the shared plumbing that turns a stored rule back into a
// sentence and a typed value back into the field's own units. Split out of PolicyEditor.tsx
// into its own module.
//
// The one safety rule in here: a rule can add to the score or take away from it, but missing
// data always leans toward keeping, and a ramp is refused rather than silently rewritten when
// its bounds run backwards.

// The ramp phrasing per field, offered as an extra choice in the condition dropdown. A phrase
// exists only where more of the number honestly means more reason to remove the title.
import { type CSSProperties, type RefObject, useEffect, useId, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Trans, useTranslation } from "react-i18next";
import { announce } from "../announce";
import { REMOVES_ITS_ROW, useRemovalFocus } from "../focus";
import {
  api,
  type Condition,
  type CustomCondemn,
  type GradedKeep,
  type ListConfig,
  type VocabField,
} from "../api";
import { count, humanDays } from "../format";
import i18next from "../i18n";
import { usePopoverShift } from "./popoverFit";
import {
  FixedQuantity,
  QuantityInput,
  sizeUnits,
  timeUnits,
  decimalsOfStep,
  useTypedNumber,
} from "./QuantityInput";
import { Segmented } from "./Segmented";
import { Notice } from "./Notice";

// Most of these fields are filtered out of the remove vocabulary today because a built-in
// signal already covers them. The map stays complete so a future field just works.
//
// A switch, not a Record<string, string>: `t()` needs a literal key, and `field.key` is
// server data, so each field gets its own literal `t()` call rather than one computed from
// the key, the same shape as `ReapBreakdown.tsx`'s `reasonLabel`.
function rampPhrase(fieldKey: string): string | undefined {
  switch (fieldKey) {
    case "days_unwatched":
      return i18next.t("policyRules.rampPhrase.daysUnwatched");
    case "size_bytes":
      return i18next.t("policyRules.rampPhrase.sizeBytes");
    case "season_rank":
      return i18next.t("policyRules.rampPhrase.seasonRank");
    case "release_age":
      return i18next.t("policyRules.rampPhrase.releaseAge");
    default:
      return undefined;
  }
}

/** A field's label, read from the catalog by its server key (`why.field.<key>`, the same
 *  subject the why-panel's condition sentences compose), never from the wire: the vocabulary
 *  endpoint does not ship one. Falls back to the raw key for a stored rule naming a field
 *  this build's catalog has no entry for.
 *
 *  Three call sites below bake this into a generated rule name, which is then stored. A rule
 *  name is operator data, not UI chrome, so it is written once, in the UI language active at
 *  that moment, and never re-translated when the operator's language changes later, the same
 *  way a list name or a policy name is never retranslated. */
function fieldLabel(key: string): string {
  const catalogKey = `why.field.${key}`;
  return i18next.exists(catalogKey) ? i18next.t(catalogKey) : key;
}

/** A field's help paragraph, from the catalog (`policyRules.fieldHelp.<key>`). Most fields
 *  carry one; `undefined` where this build's catalog has none, so the caller renders nothing
 *  rather than an empty paragraph. */
function fieldHelp(key: string): string | undefined {
  const catalogKey = `policyRules.fieldHelp.${key}`;
  return i18next.exists(catalogKey) ? i18next.t(catalogKey) : undefined;
}

/** A field's unit suffix, from the catalog (`policyRules.fieldUnit.<key>`). Most fields have
 *  none (a rank, a count, a yes/no), which read as `""`, same as the wire's old default. */
function fieldUnit(key: string): string {
  const catalogKey = `policyRules.fieldUnit.${key}`;
  return i18next.exists(catalogKey) ? i18next.t(catalogKey) : "";
}

/** The sentinel option value for a ramp phrase in the condition dropdown. */
const RAMP_OP = "__ramp__";

/** How each comparison reads in a rule sentence. A switch for the same reason as
 *  `rampPhrase` above: `op` is server data, so each op gets its own literal `t()` call. */
function opLabel(op: string): string | undefined {
  switch (op) {
    case "gte":
      return i18next.t("policyRules.opLabel.gte");
    case "lte":
      return i18next.t("policyRules.opLabel.lte");
    case "eq":
      return i18next.t("policyRules.opLabel.eq");
    case "in":
      return i18next.t("policyRules.opLabel.in");
    case "contains":
      return i18next.t("policyRules.opLabel.contains");
    default:
      return undefined;
  }
}

// Maps a vocabulary field to the built-in protection (gate) already covering it. The keep
// editor filters out a field here whenever that gate is turned on, so a custom rule never
// duplicates a switch the operator already has. Only fields with no built-in gate (size,
// all-time watchers, vote count, season rank) remain to be authored here.
const FIELD_TO_GATE: Record<string, string> = {
  days_unwatched: "min_dormancy",
  recent_watchers: "server_popularity",
  imdb_rating: "rating_floor",
  streaming_now: "streaming_now",
  // `whitelisted` and `on_curated_list` are not mapped here. Both gates are retired: every
  // list now protects through an `on_list` keep rule the operator authors, so their fields
  // stay authorable and nothing filters them.
};

// The built-in signals already cover these fields, so they are not offered as custom
// "remove" rules: the two never say the same thing twice. That leaves the new metadata
// fields (genre, requested, quality, release age, show ended) to be authored here.
const FIELD_TO_SIGNAL: Record<string, string> = {
  days_unwatched: "unwatched",
  recent_watchers: "few_watchers",
  imdb_rating: "low_rating",
  season_rank: "season_rank",
  size_bytes: "size",
};

/** The backwards-ramp complaint, named once so the six boxes that can be wrong and the one
 *  sentence explaining it are the same string rather than seven that can drift. */
const RAMP_ERROR_ID = "ramp-bounds-error";

/** Why an Add button is off, one id per editor so the three can be on screen together.
 *
 *  A `disabled` button is out of the Tab order, so a description hung on the BUTTON is
 *  unreachable by the operator it is for. The sentence binds to the empty box instead, which
 *  is both focusable and the thing that fixes it. Same shape as `RAMP_ERROR_ID` above and the
 *  password row's `errorOwner`. */
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
  if (field?.type === "bytes")
    return i18next.t("policyRules.rampValueGb", { gb: Math.round(value / 1e9) });
  if (field?.type === "days") return humanDays(value);
  if (field?.type === "rating_tenths") return (value / 10).toFixed(1);
  return withUnit(value.toLocaleString(), field ? fieldUnit(field.key) : undefined);
}

/**
 * A value and its unit, in a sentence.
 *
 * A unit that is a word takes a space ("30 days", "2 GB"). One that is punctuation does not
 * ("7.5/10"), or a saved rating keep rule would read back as "at least 7.5 /10". Deciding it
 * from the suffix rather than per branch is what keeps the next punctuation unit from
 * arriving with the same space: `/10` is the only one in `engine/fields.py` today, and
 * nothing stops a second.
 */
function withUnit(value: string, suffix: string | null | undefined): string {
  if (!suffix) return value;
  return /^[A-Za-z]/.test(suffix) ? `${value} ${suffix}` : `${value}${suffix}`;
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

/** "yes" / "no", shared by every bool value in this file: the two Segmented toggles and the
 *  sentence composers below, so the word is written once. */
function boolWord(value: boolean): string {
  return value ? i18next.t("policyRules.yesValue") : i18next.t("policyRules.noValue");
}

/** Turn a stored condition back into a sentence, in the units a person reads. */
function describeCondition(c: Condition, fields: VocabField[]): string {
  // The membership field reads as a sentence about the LIST, not as "field op value": the
  // value is the operator's own name for it. A stored rule whose list no longer exists still
  // renders, by that stored name.
  //
  // The op is still read, because `on_list` carries TEXT_OPS and a stored rule can hold one
  // other than `eq`: the composer here only ever writes `eq`, but
  // `policy_migrations.convert_list_protections` re-spells a legacy `on_curated_list` rule
  // keeping whatever op it had, and an imported body can carry one too. Ignoring the op would
  // render a stored `contains "Top"` as an exact match on a list named "Top", describing the
  // rule as narrower than it actually is, on the screen where its strength is judged.
  if (c.field === "on_list") {
    const listValue = String(c.value);
    return c.op === "eq"
      ? i18next.t("policyRules.condition.onList", { list: listValue })
      : i18next.t("policyRules.condition.onListOp", {
          op: opLabel(c.op) ?? c.op,
          list: listValue,
        });
  }
  const f = fields.find((x) => x.key === c.field);
  const label = fieldLabel(c.field);
  const op = opLabel(c.op) ?? c.op;
  let value: string;
  // Cleared by the branches that have already said their unit inside `value`.
  let unit = f ? fieldUnit(f.key) : undefined;
  if (f?.type === "rating_tenths" && typeof c.value === "number") value = (c.value / 10).toFixed(1);
  else if (f?.type === "bytes" && typeof c.value === "number") {
    value = i18next.t("policyRules.rampValueGb", { gb: Math.round(c.value / 1e9) });
    unit = "";
  } else if (f?.type === "bool") {
    value = boolWord(Boolean(c.value));
    unit = "";
  } else if (typeof c.value === "number") value = c.value.toLocaleString();
  else value = String(c.value);
  return i18next.t("policyRules.condition.generic", { label, op, value: withUnit(value, unit) });
}

function describeCondemn(rule: CustomCondemn, fields: VocabField[]): string {
  const f = fields.find((x) => x.key === rule.field);
  const label = fieldLabel(rule.field);
  if (rule.kind === "graded") {
    const phrase = rampPhrase(rule.field) ?? i18next.t("policyRules.rampPhraseDefault");
    return i18next.t("policyRules.condemn.graded", {
      label,
      phrase,
      from: rampValue(f, rule.floor),
      to: rampValue(f, rule.saturate_at),
    });
  }
  const op = opLabel(rule.op) ?? rule.op;
  const value = f?.type === "bool" ? boolWord(Boolean(rule.value)) : String(rule.value);
  return i18next.t("policyRules.condemn.simple", { label, op, value });
}

/** A value input that suggests what the library already contains, and still takes
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
   *  message that vanishes on the other two. */
  describedBy?: string | undefined;
}) {
  const { t } = useTranslation();
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
  // the early return so the hook order holds for both branches. A number field renders no list,
  // and the ref it reads is simply null.
  const popRef = useRef<HTMLUListElement>(null);
  const popShift = usePopoverShift(popRef, "suggest");
  // The arrow keys move the active option without moving DOM focus, so the browser scrolls
  // nothing on their behalf: past the seventh option of a 14rem window the operator is marking
  // a value they cannot see, and ArrowUp from the top wraps straight to the last one. Only a
  // keyboard step scrolls: `onMouseEnter` sets `highlight` too, and scrolling there would drag
  // the list out from under the pointer that is aiming at it.
  const steppedRef = useRef(false);
  useEffect(() => {
    if (!steppedRef.current) return;
    steppedRef.current = false;
    // "nearest" leaves an already-visible option where it is. The pane only moves when the
    // step left the window. Instant, like every other scroll here: smooth silently no-ops in
    // some environments, and a jump that always lands beats an animation that sometimes does
    // not, the same reasoning `DocsModal.tsx` carries.
    popRef.current?.querySelector(".suggest-opt.active")?.scrollIntoView({ block: "nearest" });
  }, [highlight]);

  // The one composed sentence in this component: a catalog field label plus a static suffix,
  // shared by all three branches below rather than composed three times.
  const valueAriaLabel = t("policyRules.suggestInput.valueAriaLabel", {
    field: fieldLabel(field.key),
  });
  const unit = fieldUnit(field.key);

  if (!isText) {
    // A number that has a unit wears it in the box, not in a placeholder that vanishes the
    // moment you type. Fields with no unit (a rank, a count) stay a plain box.
    return unit ? (
      <FixedQuantity
        value={value}
        suffix={unit}
        step={field.type === "rating_tenths" ? 0.1 : 1}
        ariaLabel={valueAriaLabel}
        describedBy={describedBy}
        onChange={(v) => onChange(String(v))}
      />
    ) : (
      <input
        type="number"
        value={value}
        aria-label={valueAriaLabel}
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
        aria-label={valueAriaLabel}
        aria-describedby={describedBy}
        value={value}
        placeholder={unit || t("policyRules.suggestInput.valuePlaceholder")}
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
 *  a ramp phrase like "the older it is". Picking the phrase swaps the value box for a
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
  const { t } = useTranslation();
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
  // The unitless ramp bounds are plain boxes, but a number being typed lives in the same one
  // place it does everywhere else: clearing one to retype must not read as a zero the next
  // digits land after.
  //
  // Their precision is the chosen field's, decided by the same tenths test the `FixedQuantity`
  // siblings in this file make when they pick a `step`. Declared once here and read for both
  // the attribute and the bound, so the spinner and the value cannot disagree. A rating bound
  // is in tenths; every other rampable field is whole, and a plain number box that allowed a
  // count ramp to start at a fraction like 1.5 would have the same defect a `FixedQuantity`
  // box avoids.
  const rampStep = field?.type === "rating_tenths" ? 0.1 : 1;
  const rampBounds = { min: 0, decimals: decimalsOfStep(rampStep) };
  const rampFrom = useTypedNumber(String(rFrom), setRFrom, rampBounds);
  const rampTo = useTypedNumber(String(rTo), setRTo, rampBounds);
  const rampable = Boolean(
    field && rampPhrase(field.key) && field.type !== "bool" && field.type !== "text",
  );
  const isRamp = rOp === RAMP_OP;
  // A ramp builds score from its low bound up to its high one, so the high one has to be
  // higher (engine/policy.py refuses floor >= saturate_at outright). This box refuses a
  // backwards ramp instead of silently correcting it: rewriting "from 5 years to 1 year" into
  // "from 5 years to 1826 days" would save a rule nobody asked for, on the lane that removes
  // files. Refuse it beside the boxes instead, and add exactly what was typed.
  const rampBackwards = isRamp && rTo <= rFrom;
  // The other reason Add is off. A ramp reads its bounds and a yes/no always has an answer, so
  // only a typed value can be missing.
  const condemnEmpty =
    !isRamp && field !== undefined && field.type !== "bool" && rValue.trim() === "";
  const helpText = field ? fieldHelp(field.key) : undefined;

  // Re-seeds the half-built rule when the operator picks a different field: its comparison,
  // its value, and its ramp bounds all belong to the old field and would otherwise carry over.
  // Depends on `rField` alone on purpose: `condemnFields` is a fresh array on every render of
  // the vocabulary query, so including it would re-run this on every keystroke and wipe the
  // value being typed. The lint rule cannot see that, hence the suppression below.
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
      const phrase = rampPhrase(field.key);
      onCondemn([
        ...condemn,
        {
          kind: "graded",
          name: uniqueName(condemn, `${fieldLabel(field.key)}: ${phrase}`),
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
          name: uniqueName(condemn, fieldLabel(field.key)),
          field: field.key,
          op: rOp,
          value: coerceValue(field, rValue),
          weight: rWeight,
        },
      ]);
    }
    // The new row appears in a list far above, the composer's boxes empty, and the Add button
    // disables itself: three ways of saying nothing. This announces only when a rule was
    // actually added, since the guard above returns silently on a no-op press, and without an
    // announcement a no-op press and a successful one would sound the same to a reader.
    announce(t("policyRules.ruleAddedAnnounce"));
    // Picking the field again is where the operator continues, and clearing it below unmounts
    // the Add button they just pressed, so without this focus call, the press that adds a rule
    // would also drop focus to `<body>`.
    fieldRef.current?.focus();
    setRField("");
    setRValue("");
  };
  // Removing a rule destroys the button holding focus. Focus goes to the next rule's Remove,
  // or to the field picker once the list is empty.
  const fieldRef = useRef<HTMLSelectElement>(null);
  const rules = useRemovalFocus(fieldRef);

  return (
    <div className="rules-card">
      <h3>{t("policyRules.removeEditor.heading")}</h3>
      <p className="blurb">{t("policyRules.removeEditor.blurb")}</p>

      {condemn.length > 0 && (
        <div className="rules-table" ref={rules.ref as RefObject<HTMLDivElement>}>
          {condemn.map((r, i) => (
            <div className="rules-row rules-row-simple" key={`c-${r.name}-${i}`}>
              <span className="rules-rule">{describeCondemn(r, condemnAll)}</span>
              {/* A yes/no rule pays its full number the moment it matches, so it says the
                  number flat. A sliding rule ramps between its floor and its top, so it
                  says "up to". Removal weights total 100, so both are literal points. */}
              <span className="rules-weight-remove">
                {r.kind === "graded"
                  ? t("policyRules.removeEditor.weightRamp", { n: r.weight })
                  : t("policyRules.removeEditor.weightFlat", { n: r.weight })}
              </span>
              {/* Named for the rule it deletes. These lists stack one "Remove" per authored
                  rule across three tables on one page. The sentence naming which rule this is
                  sits in the sibling `.rules-rule`, which no control referenced before this
                  label, so pressing the wrong button could silently delete a different rule. */}
              <button
                {...REMOVES_ITS_ROW}
                className="ghost sm"
                aria-label={t("policyRules.removeEditor.removeAriaLabel", {
                  rule: describeCondemn(r, condemnAll),
                })}
                onClick={() => {
                  rules.removing(i);
                  onCondemn(condemn.filter((_, j) => j !== i));
                }}
              >
                {t("common.remove")}
              </button>
            </div>
          ))}
        </div>
      )}

      {/* An empty picker beside no explanation reads as "there is nothing to configure here",
          which is the wrong lesson to take from a failed fetch: say what happened instead, and
          drop the form rather than offer a dropdown with nothing in it.

          Only when the picker would actually be empty, though. React Query keeps the last good
          vocabulary through a failed refetch, and `SetupWizard` fires a bare
          `invalidateQueries()` that refetches every key, so branching on the error alone would
          take the whole add-rule form away while the list behind it is still in hand. The
          vocabulary is a fixed server-defined list, not the operator's own data, so a held copy
          needs no staleness line: what a rule can look at does not change under it mid-session.

          This message does not tell the operator to reload: the rules already added are unsaved
          draft state (`PolicyEditor` binds `onCondemn` to its own `update`), and a reload would
          destroy them. "Still here" is the honest half. */}
      {condemnVocabError && !condemnVocab ? (
        <Notice tone="error">{t("policyRules.vocabLoadError")}</Notice>
      ) : (
        <>
          <div className="condition-add">
            <select
              ref={fieldRef}
              value={rField}
              aria-label={t("policyRules.fieldAriaLabel")}
              onChange={(e) => setRField(e.target.value)}
            >
              <option value="">{t("policyRules.whenPlaceholder")}</option>
              {condemnFields.map((f) => (
                <option key={f.key} value={f.key}>
                  {fieldLabel(f.key)}
                </option>
              ))}
            </select>
            {field && (
              <>
                <select
                  value={rOp}
                  aria-label={t("policyRules.comparisonAriaLabel")}
                  onChange={(e) => setROp(e.target.value)}
                >
                  {field.ops.map((o) => (
                    <option key={o} value={o}>
                      {opLabel(o) ?? o}
                    </option>
                  ))}
                  {rampable && <option value={RAMP_OP}>{rampPhrase(field.key)}</option>}
                </select>
                {isRamp ? (
                  <span className="ramp-bounds">
                    <span className="muted">{t("policyRules.removeEditor.rampFrom")}</span>
                    {/* All six boxes below carry the same pair, because the complaint is about
                        the pair: either number can be the one that is wrong, and the operator
                        may be standing on whichever they typed second. There are three
                        branches per bound below, one per `field.type`, and fixing only the
                        branch in front of you would leave the other two silent. */}
                    {field.type === "days" ? (
                      <QuantityInput
                        value={rFrom}
                        units={timeUnits()}
                        ariaLabel={t("policyRules.rampStartAriaLabel")}
                        describedBy={rampBackwards ? RAMP_ERROR_ID : undefined}
                        invalid={rampBackwards}
                        onChange={setRFrom}
                      />
                    ) : field.type === "bytes" ? (
                      <QuantityInput
                        value={rFrom}
                        units={sizeUnits()}
                        ariaLabel={t("policyRules.rampStartAriaLabel")}
                        describedBy={rampBackwards ? RAMP_ERROR_ID : undefined}
                        invalid={rampBackwards}
                        onChange={setRFrom}
                      />
                    ) : (
                      <input
                        type="number"
                        step={rampStep}
                        aria-label={t("policyRules.rampStartAriaLabel")}
                        aria-describedby={rampBackwards ? RAMP_ERROR_ID : undefined}
                        aria-invalid={rampBackwards ? true : undefined}
                        {...rampFrom}
                      />
                    )}
                    <span className="muted">{t("policyRules.removeEditor.rampTo")}</span>
                    {field.type === "days" ? (
                      <QuantityInput
                        value={rTo}
                        units={timeUnits()}
                        ariaLabel={t("policyRules.keepEditor.fullEffectAt")}
                        describedBy={rampBackwards ? RAMP_ERROR_ID : undefined}
                        invalid={rampBackwards}
                        onChange={setRTo}
                      />
                    ) : field.type === "bytes" ? (
                      <QuantityInput
                        value={rTo}
                        units={sizeUnits()}
                        ariaLabel={t("policyRules.keepEditor.fullEffectAt")}
                        describedBy={rampBackwards ? RAMP_ERROR_ID : undefined}
                        invalid={rampBackwards}
                        onChange={setRTo}
                      />
                    ) : (
                      <input
                        type="number"
                        step={rampStep}
                        aria-label={t("policyRules.keepEditor.fullEffectAt")}
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
                    label={t("policyRules.valueLabel")}
                    options={[
                      ["true", t("policyRules.yesValue")],
                      ["false", t("policyRules.noValue")],
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
                  {isRamp ? t("policyRules.upToLabel") : ""}
                  <FixedQuantity
                    value={rWeight}
                    onChange={setRWeight}
                    suffix={t("policyRules.suffixPoints")}
                    min={0}
                    max={100}
                    width="narrow"
                    ariaLabel={t("policyRules.removeEditor.pointsAriaLabel")}
                  />
                </label>
                <button className="ghost sm" onClick={add} disabled={rampBackwards || condemnEmpty}>
                  {t("policyRules.addRuleButton")}
                </button>
              </>
            )}
          </div>
          {/* Beside the boxes that fix it, and only while it is true. */}
          {rampBackwards && (
            <p className="help help-warn" id={RAMP_ERROR_ID}>
              {t("policyRules.removeEditor.rampBackwardsHelp")}
            </p>
          )}
          {condemnEmpty && (
            <p className="help help-warn" id={CONDEMN_EMPTY_ID}>
              {t("policyRules.enterValueHelp")}
            </p>
          )}
          {helpText && <p className="help">{helpText}</p>}
          <p className="help">{t("policyRules.removeEditor.helpBlurb")}</p>
        </>
      )}
    </div>
  );
}

/** The lists a membership rule can name, by the operator's own names for them. One select for
 *  both strengths: the hard composer and the lean composer offer the same choice, so they
 *  render the same control. */
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
  const { t } = useTranslation();
  return (
    <select
      value={value}
      aria-label={t("policyRules.listNameSelect.ariaLabel")}
      aria-describedby={describedBy}
      onChange={(e) => onChange(e.target.value)}
    >
      <option value="">{t("policyRules.listNameSelect.placeholder")}</option>
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
 *  can ever flag anything: the protect vocabulary is filtered server-side. */
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
  const { t } = useTranslation();
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
  // A lean ramps a number, so only numeric fields (those that accept >=) can drive one,
  // plus the membership field, whose lean is FLAT: on the list takes the full discount, off
  // it takes none. Offered first, since a list is what most leans are about.
  const onListField = allFields.find((f) => f.key === "on_list");
  const leanFields = [
    ...(onListField ? [onListField] : []),
    ...allFields.filter((f) => f.ops.includes("gte")),
  ];

  // The names a membership rule can pick from. Read only while the vocabulary actually offers
  // the field, since the server decides what is authorable, which also keeps the query out of
  // every test tree that never composes one.
  const lists = useQuery({
    queryKey: ["lists-configured"],
    queryFn: api.listConfigs,
    enabled: onListField !== undefined,
  });
  const configured = lists.data ?? [];
  // Which policy each list may protect is the server's call, not the picker's:
  // `authorable_media` is the authoritative scope (`policy_migrations.authorable_media_scope`),
  // so a Plex collection is scoped by its library kind before any sync, and an unsynced tag
  // whose type nothing can establish is offered on neither. Offering a rule on a list it could
  // never match would read as a protection the operator actually set.
  const coversThisMedia = (l: ListConfig) => l.authorable_media.includes(mediaType);
  const eligible = configured.filter(coversThisMedia);
  const allListNames = eligible.map((l) => l.name);
  // One list, one rule. A list already named by a rule is not offered again at either
  // strength: the two rules do not combine, the outright one wins, and the lean beside it can
  // never change an outcome, so the operator would be tuning points that could not matter.
  // Softening a rule means removing it, then adding it back at the other strength.
  // Case-folded, because that is how the scan matches a rule's value against a list name.
  const named = new Set(
    [
      ...conditions.filter((c) => c.field === "on_list").map((c) => String(c.value)),
      ...keeps.filter((k) => k.field === "on_list" && k.value != null).map((k) => String(k.value)),
    ].map((v) => v.trim().toLowerCase()),
  );
  // The same effect, reached by the blanket field instead of by name. `whitelisted` is a
  // yes/no over every hand-curated list at once, so an outright rule on it keeps all of them,
  // and any per-list rule beside it can never change an outcome. `named` above cannot see this
  // field, since `whitelisted` became authorable on every install once the retired gate
  // stopped filtering it out of the composer. Treated as what it is: every list already ruled.
  const keptByBlanketRule = conditions.some((c) => c.field === "whitelisted" && c.value === true);
  const listNames = keptByBlanketRule
    ? []
    : allListNames.filter((n) => !named.has(n.trim().toLowerCase()));
  // Two reasons hide a list from the picker, each with its own note, and neither is that a
  // rule already names it. A list holding only the other type can never keep here. A list no
  // sync has read yet has an unknown type and is withheld until it is checked. Counted apart
  // because the fix differs: nothing to do about the first, "check it" about the second.
  const notNamed = (l: ListConfig) => !named.has(l.name.trim().toLowerCase());
  const hiddenWrongType = keptByBlanketRule
    ? 0
    : configured.filter((l) => notNamed(l) && l.authorable_media.length > 0 && !coversThisMedia(l))
        .length;
  const hiddenUnverified = keptByBlanketRule
    ? 0
    : configured.filter((l) => notNamed(l) && l.authorable_media.length === 0).length;
  // Why the list select is empty, said once for both composers. A failed read is named as one,
  // never shown as "you have no lists". A set that all already have a rule is a third thing.
  // A set none of which this policy can keep is a fourth, which is either the wrong type or
  // not synced yet. Nothing is wrong in the last two cases, and adding another list is not the
  // fix.
  const noListsMessage =
    !lists.isPending && listNames.length === 0
      ? lists.isError
        ? t("policyRules.keepEditor.noListsLoadError")
        : keptByBlanketRule
          ? t("policyRules.keepEditor.noListsBlanketRule")
          : configured.length === 0
            ? t("policyRules.keepEditor.noListsNone")
            : eligible.length > 0
              ? // "this policy can keep" so it stays true when an unnamed wrong-type or unsynced
                // list is also hidden: those are not lists this policy can keep.
                t("policyRules.keepEditor.noListsAllTaken")
              : hiddenUnverified > 0 && hiddenWrongType === 0
                ? t("policyRules.keepEditor.noListsUnsynced")
                : hiddenWrongType > 0 && hiddenUnverified === 0
                  ? t("policyRules.keepEditor.noListsWrongType", { mediaType })
                  : t("policyRules.keepEditor.noListsGeneric")
      : null;

  const [strength, setStrength] = useState<"hard" | "lean">("hard");

  // --- keep it outright ------------------------------------------------------
  const [hField, setHField] = useState("");
  const [hOp, setHOp] = useState("");
  const [hValue, setHValue] = useState("");
  const hardField = hardFields.find((f) => f.key === hField);
  const hardIsList = hardField?.key === "on_list";
  // Why Add is off here, the condemn composer's twin. A yes/no always has an answer, so only a
  // typed value, or an unpicked list, can be missing.
  const hardEmpty = hardField !== undefined && hardField.type !== "bool" && hValue.trim() === "";
  const hardHelpText = hardField ? fieldHelp(hardField.key) : undefined;
  // The keep-rule twin of the re-seed above, and suppressed for the same reason: a new field
  // needs its own comparison and value, and `hardFields` is reallocated every render.
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
    // Same silence as the condemn composer's Add, on the same guard.
    announce(t("policyRules.ruleAddedAnnounce"));
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
  // A third variant of the same disabled-Add pattern. A numeric lean waits on its number, and
  // a membership lean waits on its list.
  const leanEmpty = leanField !== undefined && (leanIsList ? lList === "" : lAt === "");
  const leanHelpText = leanField ? fieldHelp(leanField.key) : undefined;
  const addLean = () => {
    if (!leanField || leanEmpty) return;
    if (leanIsList) {
      // Flat, not a ramp: on the list takes the full discount, off it takes none, and a
      // membership that could not be read takes the full one, fail-closed like every keep. The
      // ramp fields are inert, and floor 0 / saturate 1 is the shape the server expects. The
      // name clamps to the server's 60-character bound before `uniqueName` suffixes it.
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
          name: uniqueName(keeps, fieldLabel(leanField.key)),
          field: leanField.key,
          max_discount: lPoints,
          floor: 0,
          saturate_at: Math.max(1, saturate),
          direction: lDir,
        },
      ]);
    }
    // Same silence as the other two composers' Add.
    announce(t("policyRules.ruleAddedAnnounce"));
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
      <h3>{t("policyRules.keepEditor.heading")}</h3>
      <p className="blurb">
        <Trans
          i18nKey="policyRules.keepEditor.blurb"
          components={{ outright: <strong />, lean: <strong /> }}
        />
      </p>

      {(conditions.length > 0 || keeps.length > 0) && (
        <div className="rules-table" ref={rows.ref as RefObject<HTMLDivElement>}>
          {conditions.map((c, i) => (
            <div className="rules-row rules-row-simple" key={`h-${c.field}-${c.op}-${i}`}>
              <span className="rules-rule">
                <span className="rule-kind">
                  {t("policyRules.keepEditor.alwaysLabel")}
                  <span aria-hidden="true"> · </span>
                </span>
                {describeCondition(c, allFields)}
              </span>
              <span className="rules-weight-keep">{t("policyRules.keepEditor.keptOutright")}</span>
              <button
                {...REMOVES_ITS_ROW}
                className="ghost sm"
                aria-label={t("policyRules.keepEditor.removeConditionAriaLabel", {
                  rule: describeCondition(c, allFields),
                })}
                onClick={() => {
                  rows.removing(i);
                  onConditions(conditions.filter((_, j) => j !== i));
                }}
              >
                {t("common.remove")}
              </button>
            </div>
          ))}
          {keeps.map((k, i) => {
            const f = allFields.find((x) => x.key === k.field);
            // A membership lean is a sentence about the list, by its stored name, which still
            // renders after the list itself is gone, so the rule can be found and removed
            // rather than orphaned invisibly.
            const sentence =
              k.value != null
                ? t("policyRules.keepEditor.onListSentence", { list: k.value })
                : t("policyRules.keepEditor.leanSentence", {
                    field: fieldLabel(k.field),
                    direction: k.direction,
                    value: rampValue(f, k.saturate_at),
                  });
            // Membership is not a number, so this rule pays its whole discount or none of it.
            // "up to" is this file's own word for a ramp: `describeCondemn`'s row says the
            // number flat for a yes/no rule for exactly this reason, and this line matches it.
            const isRampLean = k.value == null;
            return (
              <div className="rules-row rules-row-simple" key={`k-${k.name}-${i}`}>
                <span className="rules-rule">
                  <span className="rule-kind">
                    {t("policyRules.keepEditor.leansLabel")}
                    <span aria-hidden="true"> · </span>
                  </span>
                  {sentence}
                </span>
                <span className="rules-weight-keep">
                  {t("policyRules.keepEditor.lowersScore", {
                    isRamp: isRampLean ? "true" : "false",
                    n: k.max_discount,
                  })}
                </span>
                <button
                  {...REMOVES_ITS_ROW}
                  className="ghost sm"
                  // The whole visible sentence, not just the field. Two lean rules on one
                  // field are a supported setup: `addLean` runs the name through `uniqueName`
                  // precisely because they collide, and the field alone would give both Remove
                  // buttons the same name. Removing the wrong one by mistake removes a keep
                  // rule, so the next scan then condemns titles the operator believed were
                  // held.
                  aria-label={t("policyRules.keepEditor.removeLeanAriaLabel", {
                    sentence,
                    isRamp: isRampLean ? "true" : "false",
                    n: k.max_discount,
                  })}
                  onClick={() => {
                    rows.removing(conditions.length + i);
                    onKeeps(keeps.filter((_, j) => j !== i));
                  }}
                >
                  {t("common.remove")}
                </button>
              </div>
            );
          })}
        </div>
      )}

      {/* Same reason as the remove editor, divided the same way: a form with an empty dropdown
          looks like a feature with nothing behind it, so name the failure and drop the form,
          but only when the dropdown really would be empty. */}
      {vocabError && !vocab ? (
        <Notice tone="error">{t("policyRules.vocabLoadError")}</Notice>
      ) : (
        <>
          <div className="rules-add">
            <Segmented
              value={strength}
              onChange={setStrength}
              label={t("policyRules.keepEditor.strengthLabel")}
              options={[
                ["hard", t("policyRules.keepEditor.strengthHard")],
                ["lean", t("policyRules.keepEditor.strengthLean")],
              ]}
            />

            {strength === "hard" ? (
              <div className="condition-add">
                <select
                  ref={hardFieldRef}
                  value={hField}
                  aria-label={t("policyRules.fieldAriaLabel")}
                  onChange={(e) => setHField(e.target.value)}
                >
                  <option value="">{t("policyRules.keepEditor.keepWhenPlaceholder")}</option>
                  {hardFields.map((f) => (
                    <option key={f.key} value={f.key}>
                      {fieldLabel(f.key)}
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
                        aria-label={t("policyRules.comparisonAriaLabel")}
                        onChange={(e) => setHOp(e.target.value)}
                      >
                        {hardField.ops.map((o) => (
                          <option key={o} value={o}>
                            {opLabel(o) ?? o}
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
                        label={t("policyRules.valueLabel")}
                        options={[
                          ["true", t("policyRules.yesValue")],
                          ["false", t("policyRules.noValue")],
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
                      {t("policyRules.addRuleButton")}
                    </button>
                  </>
                )}
                {/* Beside the box that fixes it, and bound to it: a disabled button is out of
                    the Tab order, so the sentence has to live on the box.

                    Shows noListsMessage only when the field is a list AND there are no lists
                    to offer; otherwise it shows the field's own prompt, "Pick a list" for a
                    list field or "Enter a value" for any other. Showing both at once would
                    read "Pick a list to add this rule." directly above "You have no lists
                    yet", instructing an action the second message says is impossible. */}
                {hardEmpty &&
                  (hardIsList && noListsMessage ? (
                    <p className="help help-warn" id={HARD_EMPTY_ID}>
                      {noListsMessage}
                    </p>
                  ) : (
                    <p className="help help-warn" id={HARD_EMPTY_ID}>
                      {hardIsList ? t("policyRules.pickListHelp") : t("policyRules.enterValueHelp")}
                    </p>
                  ))}
                {hardHelpText && <p className="help">{hardHelpText}</p>}
              </div>
            ) : (
              <div className="condition-add">
                <select
                  ref={leanFieldRef}
                  value={lField}
                  aria-label={t("policyRules.fieldAriaLabel")}
                  onChange={(e) => setLField(e.target.value)}
                >
                  <option value="">{t("policyRules.whenPlaceholder")}</option>
                  {leanFields.map((f) => (
                    <option key={f.key} value={f.key}>
                      {fieldLabel(f.key)}
                    </option>
                  ))}
                </select>
                {leanField && (
                  <>
                    {leanIsList ? (
                      // Flat: on the list takes the full discount, so there is no direction
                      // and no ramp end to set, since membership isn't a number.
                      <ListNameSelect
                        value={lList}
                        names={listNames}
                        onChange={setLList}
                        describedBy={leanEmpty ? LEAN_EMPTY_ID : undefined}
                      />
                    ) : (
                      <>
                        {/* Both directions stay on screen: a dropdown would let the default
                            save without the operator ever seeing there was a choice. */}
                        <Segmented
                          value={lDir}
                          onChange={setLDir}
                          label={t("policyRules.keepEditor.directionLabel")}
                          options={[
                            ["high_keeps", t("policyRules.keepEditor.directionMore")],
                            ["low_keeps", t("policyRules.keepEditor.directionLess")],
                          ]}
                        />
                        <span className="muted">{t("policyRules.keepEditor.fullEffectAt")}</span>
                        {fieldUnit(leanField.key) ? (
                          <FixedQuantity
                            value={lAt}
                            suffix={fieldUnit(leanField.key)}
                            step={leanField.type === "rating_tenths" ? 0.1 : 1}
                            ariaLabel={t("policyRules.keepEditor.fullEffectAt")}
                            describedBy={leanEmpty ? LEAN_EMPTY_ID : undefined}
                            onChange={(v) => setLAt(String(v))}
                          />
                        ) : (
                          <input
                            type="number"
                            value={lAt}
                            aria-label={t("policyRules.keepEditor.fullEffectAt")}
                            aria-describedby={leanEmpty ? LEAN_EMPTY_ID : undefined}
                            onChange={(e) => setLAt(e.target.value)}
                          />
                        )}
                      </>
                    )}
                    {/* One control standard: a number with a fixed unit is a FixedQuantity,
                        never a bare number box beside loose text. "up to" only for a ramp:
                        membership is not a number, so a list lean pays its whole discount or
                        none of it, matching `RemoveRulesEditor`'s own split. */}
                    <label className="inline-weight">
                      {leanIsList ? "" : t("policyRules.upToLabel")}
                      <FixedQuantity
                        value={lPoints}
                        onChange={setLPoints}
                        suffix={t("policyRules.suffixPointsOff")}
                        min={1}
                        max={100}
                        width="narrow"
                        ariaLabel={t("policyRules.keepEditor.pointsOffAriaLabel")}
                      />
                    </label>
                    <button className="ghost sm" onClick={addLean} disabled={leanEmpty}>
                      {t("policyRules.addRuleButton")}
                    </button>
                  </>
                )}
                {/* Shows noListsMessage only when the field is a list AND there are no lists
                    to offer; otherwise the field's own prompt, the same split as the hard
                    composer above and for the same reason. */}
                {leanEmpty &&
                  (leanIsList && noListsMessage ? (
                    <p className="help help-warn" id={LEAN_EMPTY_ID}>
                      {noListsMessage}
                    </p>
                  ) : (
                    <p className="help help-warn" id={LEAN_EMPTY_ID}>
                      {leanIsList
                        ? t("policyRules.pickListHelp")
                        : t("policyRules.enterNumberHelp")}
                    </p>
                  ))}
                {leanHelpText && <p className="help">{leanHelpText}</p>}
              </div>
            )}
          </div>
          {/* This paragraph is the operator-facing copy of what the two composers filter out,
              or a filter left unsaid would read as a list that went missing. Two filters are
              always in force and stated here. Two more, media type and not-yet-synced, hide a
              list only in some states, so their copy is the conditional notes below and the
              empty-state message above, said only when they actually apply. */}
          <p className="help">{t("policyRules.keepEditor.helpBlurb")}</p>
          {!keptByBlanketRule && listNames.length > 0 && hiddenWrongType > 0 && (
            <p className="help">
              {t("policyRules.keepEditor.hiddenWrongType", {
                n: hiddenWrongType,
                count: count(hiddenWrongType),
                mediaType,
              })}
            </p>
          )}
          {!keptByBlanketRule && listNames.length > 0 && hiddenUnverified > 0 && (
            <p className="help">
              {t("policyRules.keepEditor.hiddenUnverified", {
                n: hiddenUnverified,
                count: count(hiddenUnverified),
              })}
            </p>
          )}
        </>
      )}
    </div>
  );
}
