// SPDX-License-Identifier: AGPL-3.0-or-later
// Putting the policy page on screen, shared by the files that test it.
//
// What `PolicyEditor.test.tsx` and `PolicyEditor.warnings.test.tsx` both need to boot the
// policy page lives here, once.
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";

import type {
  CustomCondemn,
  Policy,
  PolicyBody,
  PolicyWarning,
  ProfileSettings,
  RewatchOddsFit,
  ThresholdCurve,
} from "../api";
import type { PolicySectionId } from "../components/PolicyEditor";
import { PolicyEditor } from "../components/PolicyEditor";
import { DocsProvider } from "../docs/DocsContext";
import type { ApiMock } from "./apiMock";
import { renderWithProviders } from "./renderWithProviders";

export function body(custom: CustomCondemn[] = []): PolicyBody {
  // A saved body is always on budget: the built-ins plus the operator's own rules total
  // exactly 100, which is what the server enforces.
  const builtIn = 100 - custom.reduce((sum, c) => sum + c.weight, 0);
  return {
    name: "default",
    media_type: "movie",
    condemn_at: 70,
    coverage_floor_bp: 5000,
    keep_last_seasons: 1,
    keep_first_season: false,
    keep_last_scope: "all",
    season_lookahead: 0,
    keep_in_progress: true,
    in_progress_hold_days: 30,
    keep_specials: true,
    protect_incomplete_seasons: true,
    flag_keep_conflicts: false,
    gates: [],
    signals: [
      { signal: "unwatched", weight: Math.round(builtIn * 0.7), saturate_at: 365, floor: 0 },
      { signal: "few_watchers", weight: Math.round(builtIn * 0.2), saturate_at: 3, floor: 0 },
      {
        signal: "low_rating",
        weight: builtIn - Math.round(builtIn * 0.7) - Math.round(builtIn * 0.2),
        saturate_at: 70,
        floor: 0,
      },
    ],
    protect_conditions: [],
    custom_condemn: custom,
    graded_keeps: [],
    // Off the server defaults: a fixture pinning 20/10/730 could not prove the editor passed
    // anything.
    rewatch_keep_enabled: true,
    rewatch_keep_discount: 15,
    rewatch_min_viewings: 8,
    rewatch_recent_days: 365,
    keep_rating_rules: [],
    keep_rating_match: "any",
  };
}

/** The TV half of the same policy: same shape, the season protections in play. */
export function tvBody(patch: Partial<PolicyBody> = {}): PolicyBody {
  return {
    ...body(),
    media_type: "tv",
    keep_last_seasons: 2,
    keep_first_season: true,
    keep_in_progress: true,
    signals: [
      { signal: "unwatched", weight: 60, saturate_at: 365, floor: 0 },
      { signal: "few_watchers", weight: 15, saturate_at: 3, floor: 0 },
      { signal: "season_rank", weight: 15, saturate_at: 5, floor: 0 },
      { signal: "low_rating", weight: 10, saturate_at: 70, floor: 0 },
    ],
    ...patch,
  };
}

export const pace: ProfileSettings = {
  max_items_per_run: 10,
  max_bytes_per_run: 500_000_000_000,
  max_items_per_30d: 100,
  max_bytes_per_30d: 2_000_000_000_000,
  caps_enabled: true,
  grace_days: 14,
  max_unmeasured_per_run: 0,
};

/** `App` owns both halves of where the operator is, so the address bar can name them
 *  (`/policy/tv/deletion`, navUrl.ts). This is that owner, so a rail click and the Movies/TV
 *  switch move the page here the way they do in the app. Fixed props would sit still through
 *  every click and prove nothing. */
function EditorAt({ open, media }: { open: PolicySectionId; media: "movie" | "tv" }) {
  const [section, setSection] = useState(open);
  const [mediaType, setMediaType] = useState(media);
  return (
    <PolicyEditor
      mediaType={mediaType}
      onMediaTypeChange={setMediaType}
      section={section}
      onSectionChange={setSection}
    />
  );
}

/** The two render helpers, closed over the calling file's own `apiMock`.
 *
 *  Each test file builds its mock through `vi.hoisted` so that vitest can hoist the `vi.mock`
 *  installing it above every import. A mock built here instead would be created by an import,
 *  which is exactly what the hoisted factory cannot wait for, and `apiMock.ts` says why one
 *  instance may never be shared across files in any case. */
export function policyEditorKit(apiMock: ApiMock) {
  function renderEditor(
    policy: Partial<Policy> & { body: PolicyBody },
    /** An Error renders against a profile read that failed. "pending" renders against one
     *  still in flight. The two are deliberately different states on this page. */
    paceSettings: ProfileSettings | Error | "pending" = pace,
    /** Pass an Error to render the editors against a vocabulary fetch that failed. */
    vocabulary: Error | null = null,
    /** What /policy/validate answers with. The GET's warnings are never rendered, so this
     *  is the only way a warning reaches the page. */
    validationWarnings: PolicyWarning[] = [],
    /** Where the page opens, as a cold load on `/policy/<media>/<section>` does. */
    openAt: PolicySectionId = "flags",
    openMedia: "movie" | "tv" = "movie",
    /** What GET /api/policy/rewatch-odds answers, or an Error for a failed read. Defaults to
     *  the "no scan yet" shape (empty blocks, zero total) so a test that does not care about
     *  the hold still gets a quiet card; the rewatch-odds hold's own describe block passes a
     *  seeded fit. */
    rewatchFit: RewatchOddsFit | Error = { blocks: [], total_items: 0 },
    /** What GET /api/policy/threshold-curve answers, an Error for a failed read, or
     *  "pending" for one still in flight. Defaults to `no_scan` (the safe answer for a test
     *  that does not care about the consequence sentence), which renders nothing beneath the
     *  score slider -- the same as the error and pending arms. */
    thresholdCurve: ThresholdCurve | Error | "pending" = { state: "no_scan" },
  ) {
    apiMock.policy.mockResolvedValue({
      policy_hash: "hash",
      name: "default",
      warnings: [],
      ...policy,
    });
    if (paceSettings === "pending") apiMock.profile.mockReturnValue(new Promise(() => {}));
    else if (paceSettings instanceof Error) apiMock.profile.mockRejectedValue(paceSettings);
    else apiMock.profile.mockResolvedValue(paceSettings);
    apiMock.safety.mockResolvedValue({
      destructive_enabled: false,
      has_password: true,
    });
    apiMock.scanStatus.mockResolvedValue({
      running: false,
      phase: "idle",
      done: 0,
      total: 0,
      percent: 0,
      detail: "",
      error: null,
      snapshot_id: null,
      followup_queued: false,
    });
    apiMock.seasonShape.mockResolvedValue({ total_shows: 0, season_counts: {} });
    if (rewatchFit instanceof Error) apiMock.rewatchOddsFit.mockRejectedValue(rewatchFit);
    else apiMock.rewatchOddsFit.mockResolvedValue(rewatchFit);
    if (thresholdCurve === "pending") apiMock.thresholdCurve.mockReturnValue(new Promise(() => {}));
    else if (thresholdCurve instanceof Error)
      apiMock.thresholdCurve.mockRejectedValue(thresholdCurve);
    else apiMock.thresholdCurve.mockResolvedValue(thresholdCurve);
    if (vocabulary) apiMock.vocabulary.mockImplementation(() => Promise.reject(vocabulary));
    else apiMock.vocabulary.mockResolvedValue({ lane: "condemn", fields: [] });
    apiMock.vocabularyValues.mockResolvedValue({ field: "", values: [] });
    apiMock.listConfigs.mockResolvedValue([]);
    apiMock.validatePolicy.mockResolvedValue({
      policy_hash: "hash",
      name: "default",
      body: policy.body,
      warnings: validationWarnings,
    });
    apiMock.simulate.mockResolvedValue({
      exact: true,
      stale_reason: null,
      condemned: 0,
      protected: 0,
      abstained: 0,
      reclaimable_bytes: 0,
      unknown_size_items: 0,
      hand_reaped: 0,
      newly_condemned: 0,
      no_longer_condemned: 0,
      condemned_before: 0,
      changed_titles: 0,
      histogram: [],
      examples_newly_condemned: [],
      protected_by: [],
    });
    return renderWithProviders(
      <DocsProvider>
        <EditorAt open={openAt} media={openMedia} />
      </DocsProvider>,
    );
  }

  async function renderTvEditor(
    patch: Partial<PolicyBody> = {},
    shape?: { total_shows: number; season_counts: Record<number, number> },
    /** What /policy/validate answers with, for the cases that drive a TV-only warning. */
    validationWarnings: PolicyWarning[] = [],
  ) {
    const user = userEvent.setup();
    const rendered = renderEditor({ body: tvBody(patch) }, pace, null, validationWarnings);
    if (shape) apiMock.seasonShape.mockResolvedValue(shape);
    await user.click(await screen.findByRole("button", { name: "TV" }));
    await screen.findByText("TV policy");
    return rendered;
  }

  return { renderEditor, renderTvEditor };
}
