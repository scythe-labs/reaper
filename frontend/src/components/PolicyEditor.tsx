// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The policy editor, and the live simulator beside it.
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

import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  api,
  type Condition,
  type CustomCondemn,
  type GateSetting,
  type GradedKeep,
  type Policy,
  type PolicyBody,
  type SignalSetting,
  type Simulation,
  type VocabField,
} from "../api";
import { bytes, count } from "../format";
import { QuantityInput, TIME_UNITS } from "./QuantityInput";

// Plain-English identities for every protection and signal, so the editor reads like a
// person wrote it instead of exposing the engine's field names. `unit` picks the control:
// a duration gets a value+unit picker, a rating a 0–10 box, a count a plain number.
type GateMeta = { label: string; help: string; unit?: "days" | "people" | "rating" };

const GATE_META: Record<string, GateMeta> = {
  min_dormancy: {
    label: "Give every title time to be rewatched",
    help: "Nothing is removed until it has gone at least this long without a single play. Under about three years, people still circle back to a title surprisingly often.",
    unit: "days",
  },
  server_popularity: {
    label: "Keep what your users actually watch",
    help: "If at least this many different people have played it recently, it stays — whatever it scored.",
    unit: "people",
  },
  rating_floor: {
    label: "Keep well-rated titles",
    help: "A title rated at least this high on IMDb, by enough voters, is kept.",
    unit: "rating",
  },
  others_watching: {
    label: "Protect what other people watch",
    help: "If someone other than the requester has played it, keep it — removing it would punish them for a request that wasn't theirs.",
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
    label: "Don’t judge what predates your history",
    help: "Tautulli can’t see plays from before it was installed, so anything older than your history is left alone rather than assumed unwatched.",
  },
  unmanaged: {
    label: "Only touch what Sonarr or Radarr manages",
    help: "If no *arr owns the file, Reaper has no safe way to remove it.",
  },
};

const SIGNAL_META: Record<string, { label: string; help: string }> = {
  unwatched: {
    label: "How long it’s gone unwatched",
    help: "The longer since anyone played it, the stronger the reason to remove it. The biggest single signal.",
  },
  few_watchers: {
    label: "How few people watch it",
    help: "Fewer recent watchers means more pressure to remove it.",
  },
  season_rank: {
    label: "How old a season is",
    help: "Older seasons of a show carry more pressure than the newest one.",
  },
  low_rating: {
    label: "How low it’s rated",
    help: "A poorly-rated title carries a little more pressure.",
  },
  size: {
    label: "How big it is on disk",
    help: "Off by default. Big files are usually big because they’re popular, so size makes a poor reason to delete — it only ranks titles the score has already chosen.",
  },
};

function titleCase(id: string): string {
  return id.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

/** The keep-tags: a set of *arr tags that spare a title, with an ANY/ALL switch. Chips you can
 *  remove, plus a box to add one. Shown under the "Spare titles you've tagged" protection. */
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
      {tags.length === 0 && <p className="help">No tags — this protection keeps nothing.</p>}
    </div>
  );
}

/** One protection: a switch, a plain-English label and help, and — where it has one — a
 *  threshold in the units a person thinks in. The keep-list gate also carries its tag editor. */
function GateRow({
  gate,
  keepTags,
  keepMatch,
  onKeepTags,
  onKeepMatch,
  onChange,
}: {
  gate: GateSetting;
  keepTags?: string[];
  keepMatch?: "any" | "all";
  onKeepTags?: (t: string[]) => void;
  onKeepMatch?: (m: "any" | "all") => void;
  onChange: (g: GateSetting) => void;
}) {
  const meta = GATE_META[gate.gate] ?? { label: titleCase(gate.gate), help: "" };

  return (
    <li className="rule-row">
      <label className="toggle rule-toggle">
        <input
          type="checkbox"
          checked={gate.enabled}
          onChange={(e) => onChange({ ...gate, enabled: e.target.checked })}
        />
        <span className="rule-name">{meta.label}</span>
      </label>
      {meta.help && <p className="help rule-help">{meta.help}</p>}

      {gate.gate === "whitelisted" && gate.enabled && keepTags && onKeepTags && onKeepMatch && (
        <KeepTagsEditor
          tags={keepTags}
          match={keepMatch ?? "any"}
          onTags={onKeepTags}
          onMatch={onKeepMatch}
        />
      )}

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
      {gate.enabled && meta.unit === "rating" && (
        <div className="rule-control">
          <span>IMDb</span>
          <input
            type="number"
            min={0}
            max={10}
            step={0.1}
            value={(gate.threshold / 10).toFixed(1)}
            onChange={(e) => onChange({ ...gate, threshold: Math.round(Number(e.target.value) * 10) })}
          />
          <span>from at least</span>
          <input
            type="number"
            min={0}
            step={100}
            value={gate.secondary}
            onChange={(e) => onChange({ ...gate, secondary: Number(e.target.value) || 0 })}
          />
          <span>votes</span>
        </div>
      )}
    </li>
  );
}

/** One signal: a plain-English label, its help, a slider, and a *quantified* readout — the
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

/** The owner's own protections: field + operator + value, from the protect vocabulary. Every
 *  one can only ever KEEP a title — the condemn-only fields aren't even offered here. */
function ConditionsEditor({
  conditions,
  gateIds,
  onChange,
}: {
  conditions: Condition[];
  gateIds: string[];
  onChange: (c: Condition[]) => void;
}) {
  const { data: vocab } = useQuery({
    queryKey: ["vocabulary", "protect"],
    queryFn: () => api.vocabulary("protect"),
  });
  const allFields = vocab?.fields ?? []; // for rendering existing chips' labels
  // Only offer fields that aren't already a built-in protection above.
  const fields = allFields.filter((f) => {
    const gate = FIELD_TO_GATE[f.key];
    return !gate || !gateIds.includes(gate);
  });

  const [fieldKey, setFieldKey] = useState("");
  const [op, setOp] = useState("");
  const [value, setValue] = useState("");
  const field = fields.find((f) => f.key === fieldKey);

  // Reset the operator + value to sensible defaults whenever the field changes.
  useEffect(() => {
    const f = fields.find((x) => x.key === fieldKey);
    if (!f) return;
    setOp(f.ops[0] ?? "");
    setValue(f.type === "bool" ? "true" : "");
  }, [fieldKey]); // eslint-disable-line react-hooks/exhaustive-deps

  const add = () => {
    if (!field || !op) return;
    let v: number | string | boolean;
    if (field.type === "bool") v = value === "true";
    else if (field.type === "rating_tenths") v = Math.round(Number(value) * 10);
    else if (field.type === "bytes") v = Math.round(Number(value) * 1e9);
    else if (field.type === "text") v = value;
    else v = Math.round(Number(value));
    onChange([...conditions, { field: field.key, op, value: v }]);
    setFieldKey("");
  };

  return (
    <div className="conditions">
      <h3>Your own rules</h3>
      <p className="blurb">
        Extra protections you write. Any one keeps a title, on top of the built-ins above — and
        like them, a rule can only ever <em>keep</em> something, never mark it for removal.
      </p>

      {conditions.length > 0 && (
        <ul className="condition-list">
          {conditions.map((c, i) => (
            <li key={`${c.field}-${c.op}-${i}`}>
              <span>{describeCondition(c, allFields)}</span>
              <button className="ghost sm" onClick={() => onChange(conditions.filter((_, j) => j !== i))}>
                Remove
              </button>
            </li>
          ))}
        </ul>
      )}

      <div className="condition-add">
        <select value={fieldKey} onChange={(e) => setFieldKey(e.target.value)}>
          <option value="">Keep it when…</option>
          {fields.map((f) => (
            <option key={f.key} value={f.key}>
              {f.label}
            </option>
          ))}
        </select>
        {field && (
          <>
            <select value={op} onChange={(e) => setOp(e.target.value)}>
              {field.ops.map((o) => (
                <option key={o} value={o}>
                  {OP_LABELS[o] ?? o}
                </option>
              ))}
            </select>
            {field.type === "bool" ? (
              <select value={value} onChange={(e) => setValue(e.target.value)}>
                <option value="true">yes</option>
                <option value="false">no</option>
              </select>
            ) : (
              <input
                type={field.type === "text" ? "text" : "number"}
                step={field.type === "rating_tenths" ? "0.1" : undefined}
                value={value}
                onChange={(e) => setValue(e.target.value)}
                placeholder={field.unit_suffix || "value"}
              />
            )}
            <button className="ghost sm" onClick={add} disabled={field.type !== "bool" && value === ""}>
              Add rule
            </button>
          </>
        )}
      </div>
      {field?.help_text && <p className="help">{field.help_text}</p>}
    </div>
  );
}

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

function describeCondemn(rule: CustomCondemn, fields: VocabField[]): string {
  const f = fields.find((x) => x.key === rule.field);
  const label = f?.label ?? rule.field;
  if (rule.kind === "graded") return `${label}, the higher the more likely to remove`;
  const op = OP_LABELS[rule.op] ?? rule.op;
  const value = f?.type === "bool" ? (rule.value ? "yes" : "no") : String(rule.value);
  return `${label} ${op} ${value}`;
}

/** The owner's own "reasons to remove" (custom condemn signals) and graded "leans toward
 *  keeping" -- the Radarr-style weighting, mapped onto Reaper's two lanes. A remove rule adds
 *  unsigned pressure to the score; a keep rule subtracts a discount AFTER the score and can
 *  never veto a protection. (A hard "always keep" lives in "Your own rules" above.) */
function CustomRulesEditor({
  condemn,
  keeps,
  onCondemn,
  onKeeps,
}: {
  condemn: CustomCondemn[];
  keeps: GradedKeep[];
  onCondemn: (r: CustomCondemn[]) => void;
  onKeeps: (k: GradedKeep[]) => void;
}) {
  const { data: condemnVocab } = useQuery({
    queryKey: ["vocabulary", "condemn"],
    queryFn: () => api.vocabulary("condemn"),
  });
  const { data: protectVocab } = useQuery({
    queryKey: ["vocabulary", "protect"],
    queryFn: () => api.vocabulary("protect"),
  });
  const condemnAll = condemnVocab?.fields ?? [];
  // Only the new metadata fields, not those a built-in signal already scores.
  const condemnFields = condemnAll.filter((f) => !FIELD_TO_SIGNAL[f.key]);
  // A keep ramps a number, so only numeric fields (those that accept >=) can drive one;
  // the protect vocabulary carries the useful ones (all-time watchers, vote count, ...).
  const keepFields = (protectVocab?.fields ?? []).filter((f) => f.ops.includes("gte"));

  // --- add a "remove" rule -------------------------------------------------
  const [rField, setRField] = useState("");
  const [rOp, setROp] = useState("");
  const [rValue, setRValue] = useState("");
  const [rWeight, setRWeight] = useState(20);
  const removeField = condemnFields.find((f) => f.key === rField);
  useEffect(() => {
    const f = condemnFields.find((x) => x.key === rField);
    if (!f) return;
    setROp(f.ops[0] ?? "");
    setRValue(f.type === "bool" ? "true" : "");
  }, [rField]); // eslint-disable-line react-hooks/exhaustive-deps
  const addRemove = () => {
    if (!removeField || !rOp) return;
    onCondemn([
      ...condemn,
      {
        kind: "boolean",
        name: uniqueName(condemn, removeField.label),
        field: removeField.key,
        op: rOp,
        value: coerceValue(removeField, rValue),
        weight: rWeight,
      },
    ]);
    setRField("");
    setRValue("");
  };

  // --- add a "lean toward keeping" rule ------------------------------------
  const [kField, setKField] = useState("");
  const [kPoints, setKPoints] = useState(15);
  const [kAt, setKAt] = useState("");
  const [kDir, setKDir] = useState<"high_keeps" | "low_keeps">("high_keeps");
  const keepField = keepFields.find((f) => f.key === kField);
  const addKeep = () => {
    if (!keepField || kAt === "") return;
    const saturate = Number(coerceValue(keepField, kAt));
    onKeeps([
      ...keeps,
      {
        name: uniqueName(keeps, keepField.label),
        field: keepField.key,
        max_discount: kPoints,
        floor: 0,
        saturate_at: Math.max(1, saturate),
        direction: kDir,
      },
    ]);
    setKField("");
    setKAt("");
  };

  const [effect, setEffect] = useState<"remove" | "keep">("remove");

  return (
    <div className="rules-card">
      <h3>Custom rules</h3>
      <p className="blurb">
        Nudge a title's score with your own rules. A <strong>remove</strong> rule makes a match
        more likely to be flagged; a <strong>lean toward keeping</strong> rule lowers the score
        without ever overruling a protection. Missing data only ever leans toward keeping.
      </p>

      {(condemn.length > 0 || keeps.length > 0) && (
        <div className="rules-table">
          <div className="rules-row rules-row-head">
            <span>Rule</span>
            <span>Effect</span>
            <span className="rules-weight-cell">Weight</span>
            <span />
          </div>
          {condemn.map((r, i) => (
            <div className="rules-row" key={`c-${r.name}-${i}`}>
              <span className="rules-rule">{describeCondemn(r, condemnAll)}</span>
              <span className="effect-pill effect-remove">more likely to remove</span>
              <span className="rules-weight-cell rules-weight-remove">+{r.weight}</span>
              <button
                className="ghost sm"
                onClick={() => onCondemn(condemn.filter((_, j) => j !== i))}
              >
                Remove
              </button>
            </div>
          ))}
          {keeps.map((k, i) => {
            const f = keepFields.find((x) => x.key === k.field);
            return (
              <div className="rules-row" key={`k-${k.name}-${i}`}>
                <span className="rules-rule">
                  {f?.label ?? k.field} is {k.direction === "low_keeps" ? "low" : "high"}
                </span>
                <span className="effect-pill effect-keep">lean toward keeping</span>
                <span className="rules-weight-cell rules-weight-keep">−{k.max_discount}</span>
                <button className="ghost sm" onClick={() => onKeeps(keeps.filter((_, j) => j !== i))}>
                  Remove
                </button>
              </div>
            );
          })}
        </div>
      )}

      <div className="rules-add">
        <div className="segmented" role="group" aria-label="Rule effect">
          <button
            type="button"
            className={effect === "remove" ? "seg active" : "seg"}
            onClick={() => setEffect("remove")}
          >
            More likely to remove
          </button>
          <button
            type="button"
            className={effect === "keep" ? "seg active" : "seg"}
            onClick={() => setEffect("keep")}
          >
            Lean toward keeping
          </button>
        </div>

        {effect === "remove" ? (
          <div className="condition-add">
            <select value={rField} onChange={(e) => setRField(e.target.value)}>
              <option value="">when…</option>
              {condemnFields.map((f) => (
                <option key={f.key} value={f.key}>
                  {f.label}
                </option>
              ))}
            </select>
            {removeField && (
              <>
                <select value={rOp} onChange={(e) => setROp(e.target.value)}>
                  {removeField.ops.map((o) => (
                    <option key={o} value={o}>
                      {OP_LABELS[o] ?? o}
                    </option>
                  ))}
                </select>
                {removeField.type === "bool" ? (
                  <select value={rValue} onChange={(e) => setRValue(e.target.value)}>
                    <option value="true">yes</option>
                    <option value="false">no</option>
                  </select>
                ) : (
                  <input
                    type={removeField.type === "text" ? "text" : "number"}
                    step={removeField.type === "rating_tenths" ? "0.1" : undefined}
                    value={rValue}
                    onChange={(e) => setRValue(e.target.value)}
                    placeholder={removeField.unit_suffix || "value"}
                  />
                )}
                <label className="inline-weight">
                  weight
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
                  onClick={addRemove}
                  disabled={removeField.type !== "bool" && rValue === ""}
                >
                  Add rule
                </button>
              </>
            )}
            {removeField?.help_text && <p className="help">{removeField.help_text}</p>}
          </div>
        ) : (
          <div className="condition-add">
            <select value={kField} onChange={(e) => setKField(e.target.value)}>
              <option value="">when…</option>
              {keepFields.map((f) => (
                <option key={f.key} value={f.key}>
                  {f.label}
                </option>
              ))}
            </select>
            {keepField && (
              <>
                <select
                  value={kDir}
                  onChange={(e) => setKDir(e.target.value as "high_keeps" | "low_keeps")}
                >
                  <option value="high_keeps">is high</option>
                  <option value="low_keeps">is low</option>
                </select>
                <input
                  type="number"
                  step={keepField.type === "rating_tenths" ? "0.1" : undefined}
                  value={kAt}
                  onChange={(e) => setKAt(e.target.value)}
                  placeholder={`at ${keepField.unit_suffix || "value"}`}
                />
                <label className="inline-weight">
                  keep up to
                  <input
                    type="number"
                    min={1}
                    max={100}
                    value={kPoints}
                    onChange={(e) => setKPoints(Number(e.target.value))}
                  />
                </label>
                <button className="ghost sm" onClick={addKeep} disabled={kAt === ""}>
                  Add rule
                </button>
              </>
            )}
            {keepField?.help_text && <p className="help">{keepField.help_text}</p>}
          </div>
        )}
      </div>
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
    </div>
  );
}

/** The "needs a scan" state. Informational, not an error — you didn't do anything wrong,
 *  the numbers just can't be re-derived from the old scan. So it's neutral, short, and gives
 *  you the one button that fixes it. */
function StaleNotice({ scanning, onScan }: { scanning: boolean; onScan: () => void }) {
  return (
    <div className="sim sim-info">
      <h3>Needs a fresh scan</h3>
      <p>
        You changed a weight or a protection, so the last scan's scores no longer match this
        policy. Scan to see what it would do.
      </p>
      <button className="primary sm" onClick={onScan} disabled={scanning}>
        {scanning ? "Scanning…" : "Scan now"}
      </button>
    </div>
  );
}

function Outcome({ simulation, threshold }: { simulation: Simulation; threshold: number }) {
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

      <Histogram buckets={simulation.histogram} threshold={threshold} />

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

      <p className="blurb">
        The delta is the number that matters before saving: not the total, but what changes
        relative to the list you have already reviewed.
      </p>
    </div>
  );
}

export function PolicyEditor() {
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

  // Debounce the draft the simulator/validator run against, so dragging a slider fires one
  // request when you stop -- not one per pixel. Combined with keepPreviousData below, this is
  // what stops the outcome box flickering while you adjust a weight.
  const [debounced, setDebounced] = useState<PolicyBody | null>(null);
  useEffect(() => {
    const id = setTimeout(() => setDebounced(draft), 250);
    return () => clearTimeout(id);
  }, [draft]);

  const { data: simulation } = useQuery({
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

  const startScan = async () => {
    const started = await api.startScan();
    queryClient.setQueryData(["scanStatus"], started);
  };

  const save = useMutation({
    mutationFn: (body: PolicyBody) => api.savePolicy(body),
    onSuccess: (policy: Policy) => {
      queryClient.setQueryData(["policy", mediaType], policy);
      void queryClient.invalidateQueries({ queryKey: ["policy", mediaType] });
    },
  });

  const dirty = useMemo(
    () => draft !== null && saved !== undefined && JSON.stringify(draft) !== JSON.stringify(saved.body),
    [draft, saved],
  );

  if (!draft) return <p className="muted">Loading…</p>;

  const update = (patch: Partial<PolicyBody>) => setDraft({ ...draft, ...patch });
  const totalWeight = draft.signals.reduce((sum, s) => sum + s.weight, 0);
  const invalidMessage = invalidError instanceof Error ? invalidError.message : null;

  return (
    <section className="editor">
      <div className="editor-controls">
        <div className="policy-head">
          <h2>Policy</h2>
          <div className="segmented" role="group" aria-label="Which policy">
            <button
              className={mediaType === "movie" ? "seg active" : "seg"}
              onClick={() => setMediaType("movie")}
            >
              Movies
            </button>
            <button
              className={mediaType === "tv" ? "seg active" : "seg"}
              onClick={() => setMediaType("tv")}
            >
              TV
            </button>
          </div>
        </div>
        <p className="blurb">
          {mediaType === "tv"
            ? "How Reaper judges TV — seasons, not whole shows. Tuned separately from movies."
            : "How Reaper judges your movies. TV is tuned separately — use the toggle."}
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
            Protections below still win — this only decides among titles nothing is keeping.
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
                The most recent seasons of every show are kept outright, whatever they score —
                the hard floor behind the season-rank signal below. There is no upper limit.
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
                still keeping the recent seasons of requested shows. When Reaper can’t tell whether
                a show was requested, it keeps the seasons to be safe.
              </span>
            </label>

            <label className="toggle">
              <input
                type="checkbox"
                checked={draft.keep_first_season}
                onChange={(e) => update({ keep_first_season: e.target.checked })}
              />
              <span>Always keep a show's first season, so a new viewer can still start it</span>
            </label>

            <label className="field">
              <span className="field-label">
                <span>
                  When someone’s mid-binge, also keep{" "}
                  <input
                    type="number"
                    min={0}
                    className="inline-number"
                    value={draft.season_lookahead}
                    onChange={(e) =>
                      update({ season_lookahead: Math.max(0, Number(e.target.value)) })
                    }
                  />{" "}
                  season{draft.season_lookahead === 1 ? "" : "s"} ahead
                </span>
              </span>
              <span className="help">
                Reaper protects the season a viewer is part-way through. Set this above 0 to also
                keep the seasons just ahead of where they are.
              </span>
            </label>
          </div>
        )}

        <h3>What Reaper always keeps</h3>
        <p className="blurb">
          Protections. Any one of these keeps a title no matter how it scored — and every one
          can only ever <em>keep</em> a file, never mark one for removal.
        </p>

        <ul className="rule-list">
          {draft.gates.map((gate, i) => (
            <GateRow
              key={gate.gate}
              gate={gate}
              keepTags={draft.keep_tags}
              keepMatch={draft.keep_tags_match}
              onKeepTags={(keep_tags) => update({ keep_tags })}
              onKeepMatch={(keep_tags_match) => update({ keep_tags_match })}
              onChange={(g) => {
                const gates = [...draft.gates];
                gates[i] = g;
                update({ gates });
              }}
            />
          ))}
        </ul>

        <h3>What makes a title a candidate</h3>
        <p className="blurb">
          Reasons to believe nobody will watch it again. Slide each one up if it should matter
          more. An unknown value only ever lowers a score, so an outage makes Reaper more
          cautious, never less.
        </p>

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

        <ConditionsEditor
          conditions={draft.protect_conditions}
          gateIds={draft.gates.map((g) => g.gate)}
          onChange={(protect_conditions) => update({ protect_conditions })}
        />

        <CustomRulesEditor
          condemn={draft.custom_condemn}
          keeps={draft.graded_keeps}
          onCondemn={(custom_condemn) => update({ custom_condemn })}
          onKeeps={(graded_keeps) => update({ graded_keeps })}
        />

        {/* A validation failure is an ERROR (red): this policy cannot be saved as-is. */}
        {invalidMessage && (
          <p className="notice notice-error">
            <strong>Can't save this:</strong> {invalidMessage}
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
            {save.isPending ? "Saving…" : dirty ? "Save policy" : "Saved"}
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
              policy <code>{validation.policy_hash.slice(0, 12)}</code>
            </>
          )}
        </p>

        <p className="blurb">
          Saving does not arm anything. Reaper still cannot delete, and a saved policy takes
          effect on the next scan.
        </p>
      </div>

      <div className="editor-sim">
        <h2>What this would do</h2>
        <p className="blurb">
          Re-decided against your last scan, with zero API calls. Nothing here touches Sonarr,
          Radarr or Tautulli.
        </p>
        {invalidMessage ? (
          <p className="muted">Fix the policy on the left, then this updates.</p>
        ) : simulation ? (
          simulation.exact ? (
            <Outcome simulation={simulation} threshold={draft.condemn_at} />
          ) : (
            <StaleNotice scanning={scanning} onScan={startScan} />
          )
        ) : (
          <p className="muted">Working…</p>
        )}
      </div>
    </section>
  );
}
