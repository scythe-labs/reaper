// SPDX-License-Identifier: AGPL-3.0-or-later
// How the review queue behaves when a new scan finishes while it's open:
//   - a newer scan is decided once, at the moment it appears, using the busy state at that
//     moment. A later scroll never re-decides a scan that was already handled.
//   - if the reviewer is busy at that moment, a nudge banner appears and holds their place.
//     If idle, the list refreshes quietly instead.
//   - dismissing the nudge leaves a small marker instead of silently showing a stale list.
//   - once the list catches up (any refetch), everything resets, and the next scan is decided
//     fresh.

import { act, renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { useReviewFreshness } from "./useReviewFreshness";

type Props = {
  viewSnapshotId: number | null;
  latestSnapshotId: number | null;
  isBusy: () => boolean;
  onSilentRefresh: () => void | Promise<unknown>;
  onSilentCaughtUp?: () => void;
};

const setup = (over: Partial<Props> = {}) => {
  const onSilentRefresh = over.onSilentRefresh ?? vi.fn();
  const props: Props = {
    viewSnapshotId: 42,
    latestSnapshotId: 42,
    isBusy: () => false,
    onSilentRefresh,
    ...over,
  };
  const hook = renderHook((p: Props) => useReviewFreshness(p), { initialProps: props });
  return { hook, onSilentRefresh };
};

describe("useReviewFreshness", () => {
  it("does nothing while the list matches the newest scan", () => {
    const { hook, onSilentRefresh } = setup({ viewSnapshotId: 42, latestSnapshotId: 42 });
    expect(hook.result.current.showBar).toBe(false);
    expect(hook.result.current.showMarker).toBe(false);
    expect(onSilentRefresh).not.toHaveBeenCalled();
  });

  it("refreshes quietly when a newer scan lands and the reviewer is idle", async () => {
    // This test controls both when the refresh lands and when it settles, since the test needs
    // to check state at each moment separately. The default mock would otherwise resolve on its
    // own in a microtask nothing here awaits, so the refresh would already be finished by the
    // time the assertions run, and its state update would land outside `act`.
    let settle = () => {};
    const onSilentRefresh = vi.fn(() => new Promise<void>((resolve) => (settle = resolve)));
    const props = {
      viewSnapshotId: 42,
      latestSnapshotId: 43,
      isBusy: () => false,
      onSilentRefresh,
    };
    const { hook } = setup(props);
    expect(onSilentRefresh).toHaveBeenCalledTimes(1);
    expect(hook.result.current.showBar).toBe(false);
    expect(hook.result.current.showMarker).toBe(false);
    // It lands snapshot 43 and settles: the swap happened, and nothing was ever shown.
    act(() => hook.rerender({ ...props, viewSnapshotId: 43 }));
    await act(async () => settle());
    expect(hook.result.current.showBar).toBe(false);
    expect(hook.result.current.showMarker).toBe(false);
  });

  it("raises the nudge instead of refreshing when the reviewer is mid-review", () => {
    const { hook, onSilentRefresh } = setup({
      viewSnapshotId: 42,
      latestSnapshotId: 43,
      isBusy: () => true,
    });
    expect(hook.result.current.showBar).toBe(true);
    expect(onSilentRefresh).not.toHaveBeenCalled();
  });

  it("decides once: a scroll after the nudge is up does not then refresh quietly", () => {
    const onSilentRefresh = vi.fn();
    let busy = true;
    const { hook } = setup({
      viewSnapshotId: 42,
      latestSnapshotId: 43,
      isBusy: () => busy,
      onSilentRefresh,
    });
    expect(hook.result.current.showBar).toBe(true);
    // The reviewer scrolls back to the top (no longer busy) with the same scan pending.
    busy = false;
    act(() => {
      hook.rerender({
        viewSnapshotId: 42,
        latestSnapshotId: 43,
        isBusy: () => busy,
        onSilentRefresh,
      });
    });
    expect(hook.result.current.showBar).toBe(true);
    expect(onSilentRefresh).not.toHaveBeenCalled();
  });

  it("collapses to a marker on dismiss, never a silent stale list", () => {
    const { hook } = setup({ viewSnapshotId: 42, latestSnapshotId: 43, isBusy: () => true });
    expect(hook.result.current.showBar).toBe(true);
    act(() => hook.result.current.dismiss());
    expect(hook.result.current.showBar).toBe(false);
    expect(hook.result.current.showMarker).toBe(true);
  });

  it("clears when the list catches up, and decides the next scan afresh", () => {
    const onSilentRefresh = vi.fn();
    const base = { isBusy: () => true, onSilentRefresh };
    const { hook } = setup({ viewSnapshotId: 42, latestSnapshotId: 43, ...base });
    act(() => hook.result.current.dismiss());
    expect(hook.result.current.showMarker).toBe(true);

    // The refetch lands: the list now shows snapshot 43, matching the latest. Everything resets.
    act(() => hook.rerender({ viewSnapshotId: 43, latestSnapshotId: 43, ...base }));
    expect(hook.result.current.showBar).toBe(false);
    expect(hook.result.current.showMarker).toBe(false);

    // A further scan (44) is decided fresh, independent of the earlier dismiss.
    act(() => hook.rerender({ viewSnapshotId: 43, latestSnapshotId: 44, ...base }));
    expect(hook.result.current.showBar).toBe(true);
  });

  it("stays quiet until a scan has actually completed (latest still unknown)", () => {
    const { hook, onSilentRefresh } = setup({ viewSnapshotId: 42, latestSnapshotId: null });
    expect(hook.result.current.showBar).toBe(false);
    expect(onSilentRefresh).not.toHaveBeenCalled();
  });

  it("confirms a silent swap only after the list actually catches up, never at issuance", async () => {
    const onSilentCaughtUp = vi.fn();
    // The refresh settles when its underlying refetches do, which is what the query
    // invalidation actually returns. The hook waits on that promise, not on a "fetching" flag
    // caught mid-render: a refetch that rejects within the same microtask flush never renders
    // as fetching at all, so a flag-based check would miss it and the nudge would never appear.
    let settle = () => {};
    const onSilentRefresh = vi.fn(() => new Promise<void>((resolve) => (settle = resolve)));
    const base = { isBusy: () => false, onSilentRefresh, onSilentCaughtUp };
    // Idle and a scan behind: a silent refresh is issued, but nothing is confirmed yet.
    const { hook } = setup({ viewSnapshotId: 42, latestSnapshotId: 43, ...base });
    expect(onSilentRefresh).toHaveBeenCalledTimes(1);
    expect(onSilentCaughtUp).not.toHaveBeenCalled();
    // It lands snapshot 43, and then settles: only now is the swap confirmed.
    act(() => hook.rerender({ viewSnapshotId: 43, latestSnapshotId: 43, ...base }));
    await act(async () => {
      settle();
    });
    expect(onSilentCaughtUp).toHaveBeenCalledTimes(1);
    expect(hook.result.current.showBar).toBe(false);
  });

  it("nudges, and does not confirm, when a silent refresh settles with the list still behind", async () => {
    const onSilentCaughtUp = vi.fn();
    let settle = () => {};
    const onSilentRefresh = vi.fn(() => new Promise<void>((resolve) => (settle = resolve)));
    const base = { isBusy: () => false, onSilentRefresh, onSilentCaughtUp };
    const { hook } = setup({ viewSnapshotId: 42, latestSnapshotId: 43, ...base });
    // Nothing shows yet: the refetch is still in flight, and a nudge here would be wrong.
    expect(hook.result.current.showBar).toBe(false);
    // It finishes with the list STILL on snapshot 42 (a network blip).
    await act(async () => {
      settle();
    });
    expect(onSilentCaughtUp).not.toHaveBeenCalled();
    expect(hook.result.current.showBar).toBe(true);
  });

  // Two silent refreshes on one mount, tracked independently: a flag that just says "something
  // has settled" would work for the first refresh but stay permanently true after it, hiding
  // whether the second one has confirmed anything.
  describe("a second silent refresh on the same mount", () => {
    /** Idle, with each refresh's settle held so the test decides when it lands. */
    const twoRefreshes = () => {
      const onSilentCaughtUp = vi.fn();
      const settles: Array<() => void> = [];
      const onSilentRefresh = vi.fn(() => new Promise<void>((resolve) => settles.push(resolve)));
      return { settles, base: { isBusy: () => false, onSilentRefresh, onSilentCaughtUp } };
    };

    it("is confirmed on its own catch-up, not nudged at issuance", async () => {
      const { settles, base } = twoRefreshes();
      // Scan 43 lands while idle: refresh #1, the list catches up, and it settles.
      const { hook } = setup({ viewSnapshotId: 42, latestSnapshotId: 43, ...base });
      act(() => hook.rerender({ viewSnapshotId: 43, latestSnapshotId: 43, ...base }));
      await act(async () => settles[0]!());
      expect(base.onSilentCaughtUp).toHaveBeenCalledTimes(1);

      // Scan 44 lands, still idle: refresh #2 goes out and nothing is claimed about it yet.
      act(() => hook.rerender({ viewSnapshotId: 43, latestSnapshotId: 44, ...base }));
      expect(base.onSilentRefresh).toHaveBeenCalledTimes(2);
      expect(hook.result.current.showBar).toBe(false);

      // It lands snapshot 44 and settles: the swap is confirmed a second time.
      act(() => hook.rerender({ viewSnapshotId: 44, latestSnapshotId: 44, ...base }));
      await act(async () => settles[1]!());
      expect(base.onSilentCaughtUp).toHaveBeenCalledTimes(2);
      expect(hook.result.current.showBar).toBe(false);
    });

    it("still nudges when it settles with the list behind", async () => {
      const { settles, base } = twoRefreshes();
      const { hook } = setup({ viewSnapshotId: 42, latestSnapshotId: 43, ...base });
      act(() => hook.rerender({ viewSnapshotId: 43, latestSnapshotId: 43, ...base }));
      await act(async () => settles[0]!());

      // Scan 44, and this time the refresh finishes with the list still on 43 (a network blip).
      act(() => hook.rerender({ viewSnapshotId: 43, latestSnapshotId: 44, ...base }));
      expect(hook.result.current.showBar).toBe(false);
      await act(async () => settles[1]!());
      expect(hook.result.current.showBar).toBe(true);
      expect(base.onSilentCaughtUp).toHaveBeenCalledTimes(1);
    });
  });

  it("still nudges when the refresh never renders as fetching at all", async () => {
    // A refetch that rejects synchronously never has a moment where a "fetching" flag would
    // read true, so a hook that watches such a flag would leave the reviewer on a stale list
    // with nothing on screen to say so.
    const onSilentCaughtUp = vi.fn();
    const base = {
      isBusy: () => false,
      onSilentRefresh: vi.fn(() => Promise.resolve()),
      onSilentCaughtUp,
    };
    const { hook } = setup({ viewSnapshotId: 42, latestSnapshotId: 43, ...base });
    await act(async () => {});
    expect(onSilentCaughtUp).not.toHaveBeenCalled();
    expect(hook.result.current.showBar).toBe(true);
  });
});
