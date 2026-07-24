// SPDX-License-Identifier: AGPL-3.0-or-later
// The overlay contract for a hand decision, from the mutation's side:
//   - a PER-ITEM decision (movie/season already on screen) is patched by media_key and the
//     active tab is NOT refetched, so the just-decided row stays put and re-buckets on the
//     next fetch;
//   - a WHOLE-SHOW decision keys on the show/group key: it patches the show-level fields on the
//     group's loaded seasons and, likewise, does NOT refetch the active tab -- so the card
//     carrying the control reflects the decision AND the show stays in the lane the operator is
//     looking at (a whole-show reap must not re-bucket a Limbo show to Condemned and vanish
//     mid-review);
//   - a decision on a row/show that is NOT loaded matches nothing and falls back to a real
//     refetch, since there is no on-screen overlay to preserve.
// This is the regression guard for both the whole-show "no feedback" gap and the whole-show
// "jumps out of the list" regression.

import { act, renderHook } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createElement, type ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "./api";
import { useOverrideMutations } from "./useOverrideMutations";

vi.mock("./api", () => ({
  api: { override: vi.fn().mockResolvedValue({}), clearOverride: vi.fn().mockResolvedValue({}) },
}));

// A minimal candidate row -- only the fields the patch reads/writes matter here.
const row = (media_key: string, group_key: string | null) =>
  ({
    media_key,
    group_key,
    override: null,
    override_own: null,
    spared: false,
    show_override: null,
    show_spare_expires_at: null,
  }) as never;

const seedCandidates = (client: QueryClient) =>
  client.setQueryData(["candidates", "condemn", "", {}], {
    pages: [{ items: [row("radarr:1:1", null), row("sonarr:1:9:2", "sonarr:1:9")] }],
    pageParams: [0],
  });

const cachedRow = (client: QueryClient, key: string) => {
  const data = client.getQueryData(["candidates", "condemn", "", {}]) as {
    pages: {
      items: { media_key: string; override: string | null; show_override: string | null }[];
    }[];
  };
  return data.pages[0]!.items.find((c) => c.media_key === key)!;
};

// The refetchType the queue was invalidated with on the last call touching ["candidates"].
const queueRefetchType = (spy: ReturnType<typeof vi.spyOn>) => {
  const calls = spy.mock.calls as unknown[][];
  const queueCalls = calls.filter(
    (c) => (c[0] as { queryKey?: unknown[] })?.queryKey?.[0] === "candidates",
  );
  return (queueCalls.at(-1)?.[0] as { refetchType?: string })?.refetchType;
};

const setup = () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  seedCandidates(client);
  const invalidateSpy = vi.spyOn(client, "invalidateQueries");
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client }, children);
  const hook = renderHook(() => useOverrideMutations(), { wrapper });
  return { client, invalidateSpy, hook };
};

describe("useOverrideMutations", () => {
  beforeEach(() => vi.clearAllMocks());

  it("patches a per-item decision in place and does NOT refetch the active tab", async () => {
    const { client, invalidateSpy, hook } = setup();
    await act(async () => {
      await hook.result.current.setOverride.mutateAsync({ key: "radarr:1:1", decision: "spare" });
    });
    // The row was patched where it sits, keeping it on screen.
    expect(cachedRow(client, "radarr:1:1").override).toBe("spare");
    // ...and the active queue was left alone, so it stays put.
    expect(queueRefetchType(invalidateSpy)).toBe("none");
  });

  it("patches the show-level overlay on a whole-show decision and does NOT refetch", async () => {
    const { client, invalidateSpy, hook } = setup();
    await act(async () => {
      await hook.result.current.setOverride.mutateAsync({ key: "sonarr:1:9", decision: "spare" });
    });
    // The group key matches the season's group_key, so the SHOW-LEVEL field is patched...
    expect(cachedRow(client, "sonarr:1:9:2").show_override).toBe("spare");
    // ...while the season's OWN override is untouched (the whole-show decision inherits down).
    expect(cachedRow(client, "sonarr:1:9:2").override).toBeNull();
    // ...and the active queue is NOT refetched, so the show stays in its current lane.
    expect(queueRefetchType(invalidateSpy)).toBe("none");
  });

  it("drops the show-level overlay on a whole-show clear and does NOT refetch", async () => {
    const { client, invalidateSpy, hook } = setup();
    await act(async () => {
      await hook.result.current.setOverride.mutateAsync({ key: "sonarr:1:9", decision: "reap" });
    });
    expect(cachedRow(client, "sonarr:1:9:2").show_override).toBe("reap");
    await act(async () => {
      await hook.result.current.clearOverride.mutateAsync("sonarr:1:9");
    });
    // The show-level overlay is gone, and the show still stays put (settles on next fetch).
    expect(cachedRow(client, "sonarr:1:9:2").show_override).toBeNull();
    expect(queueRefetchType(invalidateSpy)).toBe("none");
  });

  it("refetches the active tab when clearing an override on a not-loaded row", async () => {
    const { invalidateSpy, hook } = setup();
    await act(async () => {
      await hook.result.current.clearOverride.mutateAsync("radarr:1:404");
    });
    expect(api.clearOverride).toHaveBeenCalledWith("radarr:1:404");
    expect(queueRefetchType(invalidateSpy)).toBe("active");
  });
});
