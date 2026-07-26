// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The policy editor's numbers: the three presets, the shipped signal mixes they reset to, the
// arithmetic that keeps the removal lane totalling 100, and the test for whether a draft is
// still one of the presets.
//
// Pulled out of PolicyEditor.tsx, which was a single 2,700-line module holding these, five
// rule editors and a 1,000-line component (R-2). None of it renders; all of it is pure, which
// is why the tests can call it directly.

import type { PolicyBody, ProfileSettings } from "../api";

// ---------------------------------------------------------------------------
// Presets: three starting points that stage (never save) the threshold and the
// pace. Weights are RESET to the shipped mix on apply -- a preset is a known
// place to start from, not a tweak -- and the operator's own removal rules are
// rescaled alongside it (rescaleToBudget) so the lane still totals 100. The
// badge only claims a preset while the draft actually matches it.
// ---------------------------------------------------------------------------

export type PresetId = "cautious" | "balanced" | "aggressive";

/** The shipped signal mixes (see engine/policy.py defaults). A preset resets to these. */
export const DEFAULT_WEIGHTS: Record<"movie" | "tv", Record<string, number>> = {
  movie: { unwatched: 70, few_watchers: 20, low_rating: 10 },
  tv: { unwatched: 60, few_watchers: 15, season_rank: 15, low_rating: 10 },
};

export type PresetCaps = Pick<
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

export const PRESETS: { id: PresetId; label: string; help: string; condemn_at: number; caps: PresetCaps }[] = [
  {
    id: "cautious",
    label: "Cautious",
    help: "Cautious: only flags a title it is very sure about, removes less per run, and shows a title as leaving for a month.",
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
export const REMOVAL_POINTS = 100;

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
export function rescaleToBudget(weights: number[]): number[] {
  return rescaleTo(weights, REMOVAL_POINTS);
}

/** The arithmetic above, to any total. Split out so the preset badge can scale the shipped
 *  mix to whatever total a draft's built-ins currently carry and compare like with like
 *  (weightsMatchMix); the budget is the only target the editor itself ever asks for. */
export function rescaleTo(weights: number[], target: number): number[] {
  const total = weights.reduce((sum, w) => sum + w, 0);
  if (total <= 0) return weights;
  const exact = weights.map((w) => (w * target) / total);
  const floors = exact.map((x) => Math.floor(x));
  const spare = target - floors.reduce((sum, f) => sum + f, 0);
  // Largest fractional remainder first; ties keep their original order, as the server's
  // stable sort does.
  const order = floors
    .map((_, i) => i)
    .sort((a, b) => exact[b]! - floors[b]! - (exact[a]! - floors[a]!));
  for (const i of order.slice(0, spare)) floors[i]! += 1;
  return floors;
}

/** Whether the built-in weights still have the shipped mix's SHAPE.
 *
 *  Exact equality was the old test, and it broke the moment the operator had one rule of
 *  their own: applyPreset writes the mix and then rescales the WHOLE removal lane back to
 *  the 100-point budget, so what gets stored is the mix times a factor, never the mix
 *  itself. Clicking "Cautious" with a custom rule lit no segment at all and flipped the
 *  help line to "Custom: your own tuning, not a preset." on the very click that applied
 *  it, contradicted by the "Staged, not saved" line right below (U-8).
 *
 *  Rescaling is score-preserving, so the same shape IS the same preset. With nothing to
 *  rescale the comparison stays exact, to the point. Otherwise the mix is scaled to the
 *  draft's own built-in total with the same largest-remainder arithmetic, and allowed to
 *  differ by the one point that arithmetic can itself move a weight -- below that a hand
 *  tune and a rounding are the same number, and no test can tell them apart.
 */
export function weightsMatchMix(draft: PolicyBody, mix: Record<string, number>): boolean {
  const want = draft.signals.map((s) => mix[s.signal] ?? 0);
  const have = draft.signals.map((s) => s.weight);
  const haveTotal = have.reduce((sum, w) => sum + w, 0);
  const wantTotal = want.reduce((sum, w) => sum + w, 0);
  // No built-in weight at all is a shape, but it is not a preset's: every preset ships a
  // mix that carries points. Refuse rather than let a degenerate all-zero draft match.
  if (haveTotal <= 0 || wantTotal <= 0) return false;
  if (haveTotal === wantTotal) return have.every((w, i) => w === want[i]);
  const scaled = rescaleTo(want, haveTotal);
  return have.every((w, i) => Math.abs(w - (scaled[i] ?? 0)) <= 1);
}

/** Which preset the draft currently IS, or null for "Custom". Honest by construction: a
 *  preset badge is only shown while the threshold matches it AND the built-ins still carry
 *  the shipped mix's shape -- hand-tuned weights always read as Custom. */
export function activePreset(draft: PolicyBody): PresetId | null {
  const mix = DEFAULT_WEIGHTS[draft.media_type === "tv" ? "tv" : "movie"];
  if (!weightsMatchMix(draft, mix)) return null;
  return PRESETS.find((p) => p.condemn_at === draft.condemn_at)?.id ?? null;
}

/** A list said the way a person would: "A", "A and B", "A, B, and C". Used by the intent
 *  summary, whose clauses are pushed one at a time as their switches turn on, so the
 *  number of them is never known ahead of time. */
export function andList(parts: string[]): string {
  if (parts.length <= 1) return parts[0] ?? "";
  if (parts.length === 2) return `${parts[0]} and ${parts[1]}`;
  return `${parts.slice(0, -1).join(", ")}, and ${parts[parts.length - 1]}`;
}
