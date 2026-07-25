// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The PIN poll's run guard. Stopping the timer never stopped a request already in the air,
// so an answer that landed after the sign-in had settled still called the handlers: the
// operator watched "Plex sign-in failed" paint over a session that had just succeeded, or
// got signed in a moment after pressing Cancel (B-11).
//
// These drive the hook directly. Both cases need two polls in flight at once with control
// over which settles first, which is not something a rendered panel lets you arrange.

import { act, renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { PinPollResult } from "./PlexPin";
import { usePlexPinPoll } from "./PlexPin";

/** A poll that never answers on its own, so the test decides when (and whether) each
 *  request in flight comes back. `answer` settles the oldest one still outstanding. */
function deferredPolls() {
  const pending: { ok: (r: PinPollResult) => void; fail: (e: Error) => void }[] = [];
  const poll = vi.fn(
    () =>
      new Promise<PinPollResult>((resolve, reject) => {
        pending.push({ ok: resolve, fail: reject });
      }),
  );
  const answer = () => {
    const next = pending.shift();
    if (!next) throw new Error("no poll is in flight");
    return next;
  };
  return { poll, answer };
}

describe("a poll that lands after the sign-in has settled", () => {
  it("never stacks polls, and ignores one that answers after the run ended", async () => {
    vi.useFakeTimers();
    try {
      const { poll, answer } = deferredPolls();
      const onFailed = vi.fn();
      const { result } = renderHook(() =>
        usePlexPinPoll({ poll, onOk: vi.fn(), onFailed, onTimedOut: vi.fn() }),
      );

      act(() => result.current.begin(42));

      // Three ticks with plex.tv not answering. Exactly ONE request is in the air: the
      // pile-up is what made two answers race in the first place, so the tick skips while
      // one is outstanding rather than adding to it.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(6000);
      });
      expect(poll).toHaveBeenCalledTimes(1);

      // The run ends while that one is still out.
      act(() => result.current.cancel());

      // It comes back a rejection -- a consumed PIN, or a 429. Clearing the timer never
      // stopped this request, and the handler used to run regardless, painting "Plex
      // sign-in failed" over a screen that had already moved on.
      await act(async () => {
        answer().fail(new Error("PIN already used"));
      });
      expect(onFailed).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it("cannot sign the operator in after they pressed Cancel", async () => {
    const { poll, answer } = deferredPolls();
    const onOk = vi.fn();
    const { result } = renderHook(() =>
      usePlexPinPoll({ poll, onOk, onFailed: vi.fn(), onTimedOut: vi.fn() }),
    );

    // The picker is up (a pick polls immediately rather than waiting for a tick), so the
    // hook needs a PIN in hand first.
    act(() => result.current.begin(42));
    let picked: Promise<void> = Promise.resolve();
    act(() => {
      picked = result.current.pick("machine-1");
    });
    expect(poll).toHaveBeenCalledWith(42, "machine-1");

    // The operator gives up while plex.tv is still thinking.
    act(() => result.current.cancel());

    await act(async () => {
      answer().ok({ status: "ok", servers: null });
      await picked;
    });
    expect(onOk).not.toHaveBeenCalled();
  });
});
