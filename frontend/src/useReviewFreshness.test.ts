// SPDX-License-Identifier: AGPL-3.0-or-later
// The rule that keeps the review queue honest when a scan finishes underneath it. Load-bearing:
//   - a newer scan is decided ONCE, at the instant it appears, from the busy state THEN --
//     a later scroll must not re-decide a scan already handled;
//   - busy at that instant holds the reviewer's place (a nudge); idle refreshes quietly;
//   - dismiss defers to a marker, never a silent stale list;
//   - the list catching up (any refetch) resets everything, and the next scan decides afresh.

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

  it("refreshes quietly when a newer scan lands and the reviewer is idle", () => {
    const { hook, onSilentRefresh } = setup({
      viewSnapshotId: 42,
      latestSnapshotId: 43,
      isBusy: () => false,
    });
    expect(onSilentRefresh).toHaveBeenCalledTimes(1);
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

    // A further scan (44) is a fresh decision, not suppressed by the earlier dismiss.
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
    // The refresh settles when its refetches do -- what the queue's invalidation returns. The
    // hook waits on THIS, not on a fetching flag it happens to catch mid-render: a refetch that
    // rejects in the same microtask flush never renders as fetching at all, and the nudge below
    // silently never came (found when the queue stopped re-rendering so freely, P-1/P-7).
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
    // Nothing yet -- the refetch is still in flight, and an early nudge would be a lie.
    expect(hook.result.current.showBar).toBe(false);
    // It finishes with the list STILL on snapshot 42 (a network blip).
    await act(async () => {
      settle();
    });
    expect(onSilentCaughtUp).not.toHaveBeenCalled();
    expect(hook.result.current.showBar).toBe(true);
  });

  it("still nudges when the refresh never renders as fetching at all", async () => {
    // The regression this signal exists for: a refetch that rejects synchronously. There is no
    // window in which any flag reads "fetching", so a hook watching one would leave the reviewer
    // on a stale list with nothing on screen to say so (PR-5).
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
