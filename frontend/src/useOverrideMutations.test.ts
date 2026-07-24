// SPDX-License-Identifier: AGPL-3.0-or-later
// The overlay contract for a hand decision, from the mutation's side:
//   - a PER-ITEM decision (movie/season already on screen) is patched in place and the active
//     tab is NOT refetched, so the just-decided row stays put and re-buckets on the next fetch;
//   - a WHOLE-SHOW decision keys on the show/group key, which no row's media_key equals, so it
//     patches nothing -- and then the active tab IS refetched, so the card that carries the
//     control actually reflects the decision instead of silently bouncing back.
// This is the regression guard for the whole-show "no feedback" gap.

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
  ({ media_key, group_key, override: null, override_own: null, spared: false }) as never;

const seedCandidates = (client: QueryClient) =>
  client.setQueryData(["candidates", "condemn", "", {}], {
    pages: [{ items: [row("radarr:1:1", null), row("sonarr:1:9:2", "sonarr:1:9")] }],
    pageParams: [0],
  });

const cachedRow = (client: QueryClient, key: string) => {
  const data = client.getQueryData(["candidates", "condemn", "", {}]) as {
    pages: { items: { media_key: string; override: string | null }[] }[];
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

  it("refetches the active tab for a whole-show decision (no row matches the group key)", async () => {
    const { client, invalidateSpy, hook } = setup();
    await act(async () => {
      await hook.result.current.setOverride.mutateAsync({ key: "sonarr:1:9", decision: "spare" });
    });
    // The show/group key matches no row's media_key, so nothing is patched in place...
    expect(cachedRow(client, "sonarr:1:9:2").override).toBeNull();
    // ...and the active queue is refetched so the show card reflects the decision.
    expect(queueRefetchType(invalidateSpy)).toBe("active");
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
