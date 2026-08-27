// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The policy editor's numbers: the three presets, the shipped signal mixes they reset to, the
// arithmetic that keeps the removal lane totalling 100, and the test for whether a draft is
// still one of the presets.
//
// Split out of PolicyEditor.tsx into its own module. None of it renders; all of it is pure,
// which is why the tests can call it directly.
//
// The preset labels and help sentences live in `locales/en/ui.json` under `policyMeta.*`.
// This is a data module, not a component, so it reads the catalog through the plain
// `i18next` import rather than the `useTranslation` hook. `presets` is a FUNCTION, never a
// constant: a string resolved in a module body keeps whatever language was serving when the
// module first loaded (`i18n-module-scope.test.ts`).

import type { PolicyBody, ProfileSettings } from "../api";
import i18next from "../i18n";

// ---------------------------------------------------------------------------
// Presets: three starting points that stage, never save, the threshold and the pace. Weights
// reset to the shipped mix on apply, since a preset is a known place to start from, not a
// tweak, and the operator's own removal rules are rescaled alongside it (rescaleToBudget) so
// the lane still totals 100. The badge only claims a preset while the draft actually matches
// it.
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
  // Every preset promises enforcement ("removes less per run"), so it must also turn the caps
  // on. Staging the numbers while leaving caps off would save an uncapped profile.
  | "caps_enabled"
  // Turning the caps on activates `policy._run_cap_within_rolling_cap`, which early-returns
  // while they are off, so a preset that writes only a subset of the caps can produce a
  // combination the operator was never allowed to store directly: an allowance of up to 25
  // unmeasured items combined with Cautious's 5-items-per-run cap fails the validator, which
  // refuses a run that may delete more unmeasured items than items. Naming every cap the
  // validator reads here means a preset can only ever produce a combination it has stated in
  // full.
  | "max_unmeasured_per_run"
>;

export const presets = (): {
  id: PresetId;
  label: string;
  help: string;
  condemn_at: number;
  caps: PresetCaps;
}[] => [
  {
    id: "cautious",
    label: i18next.t("policyMeta.presets.cautious.label"),
    help: i18next.t("policyMeta.presets.cautious.help"),
    condemn_at: 82,
    caps: {
      max_items_per_run: 5,
      max_bytes_per_run: 250_000_000_000,
      max_items_per_30d: 50,
      max_bytes_per_30d: 1_000_000_000_000,
      grace_days: 30,
      caps_enabled: true,
      // The shipped default, on all three: no preset's help text says anything about items
      // whose size nothing will report, so none of them may quietly admit any. It is also the
      // only value that is legal under every preset's per-run item cap, and the value that
      // holds those items back, since a preset resolves toward keeping the file.
      max_unmeasured_per_run: 0,
    },
  },
  {
    id: "balanced",
    label: i18next.t("policyMeta.presets.balanced.label"),
    help: i18next.t("policyMeta.presets.balanced.help"),
    condemn_at: 70,
    caps: {
      max_items_per_run: 10,
      max_bytes_per_run: 500_000_000_000,
      max_items_per_30d: 100,
      max_bytes_per_30d: 2_000_000_000_000,
      grace_days: 14,
      caps_enabled: true,
      max_unmeasured_per_run: 0,
    },
  },
  {
    id: "aggressive",
    label: i18next.t("policyMeta.presets.aggressive.label"),
    help: i18next.t("policyMeta.presets.aggressive.help"),
    condemn_at: 58,
    caps: {
      max_items_per_run: 25,
      max_bytes_per_run: 1_000_000_000_000,
      max_items_per_30d: 150,
      max_bytes_per_30d: 4_000_000_000_000,
      grace_days: 7,
      caps_enabled: true,
      max_unmeasured_per_run: 0,
    },
  },
];

/** The removal budget every policy must total, matching the server
 *  (PolicyBody._weights_total_one_hundred). */
export const REMOVAL_POINTS = 100;

/** A set of removal weights rescaled so they total exactly REMOVAL_POINTS, using the same
 *  largest-remainder arithmetic the server uses to repair a stored policy
 *  (engine/policy_migrations.rebalance). Score-preserving: the score is already
 *  100 * (weighted signal total / total weight), so scaling every weight by one factor
 *  cannot move it, and largest-remainder keeps the rounding under a point.
 *
 *  A preset needs this because the shipped mix alone is already the whole budget, so without
 *  it, any removal rule of the operator's own would push the draft over budget and block Save
 *  for the pace draft too.
 *
 *  Returns the weights unchanged when there is nothing to scale (no weight at all), which the
 *  budget readout then reports as under budget.
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
  // Largest fractional remainder first. Ties keep their original order, the same way the
  // server's stable sort does.
  const order = floors
    .map((_, i) => i)
    .sort((a, b) => exact[b]! - floors[b]! - (exact[a]! - floors[a]!));
  for (const i of order.slice(0, spare)) floors[i]! += 1;
  return floors;
}

/** Whether the built-in weights still have the shipped mix's shape.
 *
 *  Exact equality is not enough: applyPreset writes the mix and then rescales the whole
 *  removal lane back to the 100-point budget, so what gets stored is the mix times a factor,
 *  never the mix itself. Once the operator has one removal rule of their own, a preset's
 *  weights are never exactly equal to the shipped mix, even right after applying it.
 *
 *  Rescaling is score-preserving, so the same shape is the same preset. With nothing to
 *  rescale, the comparison stays exact. Otherwise the mix is scaled to the draft's own
 *  built-in total with the same largest-remainder arithmetic, and allowed to differ by the
 *  one point that arithmetic can itself move a weight: below that, a hand tune and a
 *  rounding are the same number, and no test can tell them apart.
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
 *  the shipped mix's shape. Hand-tuned weights always read as Custom. */
export function activePreset(draft: PolicyBody): PresetId | null {
  const mix = DEFAULT_WEIGHTS[draft.media_type === "tv" ? "tv" : "movie"];
  if (!weightsMatchMix(draft, mix)) return null;
  return presets().find((p) => p.condemn_at === draft.condemn_at)?.id ?? null;
}
