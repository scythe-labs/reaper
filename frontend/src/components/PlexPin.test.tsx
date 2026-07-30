// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The PIN poll's run guard. Stopping the timer never stopped a request already in the air,
// so an answer that landed after the sign-in had settled still called the handlers: the
// operator watched "Plex sign-in failed" paint over a session that had just succeeded, or
// got signed in a moment after pressing Cancel (B-11).
//
// These drive the hook directly. Both cases need two polls in flight at once with control
// over which settles first, which is not something a rendered panel lets you arrange.

import { act, render, renderHook, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { announce, Announcer } from "../announce";
import type { PinPollResult } from "./PlexPin";
import { usePlexPinPoll } from "./PlexPin";

// The real `announce` and the real `Announcer`, with a counter around the call. Counting matters
// here and the rendered regions cannot do it: they alternate, hold one sentence at a time, and
// replace it with the next -- so the same sentence said once and said three times leaves exactly
// the same DOM. Spreading the actual module keeps the region real, so the "is it spoken at all"
// half is still proven against markup rather than against a spy (rule 119).
vi.mock("../announce", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../announce")>();
  return { ...actual, announce: vi.fn(actual.announce) };
});

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

describe("a wait that is taking longer than usual", () => {
  // Every transition in this flow is driven by the two-second poll and not by the operator, so
  // the waiting paragraph swapping to "your server is restarting" is a change nobody touched
  // anything to cause: on screen it changed, by ear it did not (#177). `announce()` only speaks
  // through a mounted `Announcer`, so the region is mounted here rather than assumed.
  const REASON = "Your Plex server is restarting. Reaper is still waiting.";

  beforeEach(() => {
    vi.mocked(announce).mockClear();
  });

  /** The spoken text, from the two polite regions `Announcer` alternates between. */
  const said = () =>
    screen
      .getAllByRole("status")
      .map((n) => n.textContent)
      .filter((t) => t !== "");

  function pollingHook() {
    const { poll, answer } = deferredPolls();
    render(<Announcer />);
    const { result } = renderHook(() =>
      usePlexPinPoll({ poll, onOk: vi.fn(), onFailed: vi.fn(), onTimedOut: vi.fn() }),
    );
    act(() => result.current.begin(42));
    return { answer, result };
  }

  it("says the reason out loud when the poll first reports it", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const { answer, result } = pollingHook();

      await act(async () => {
        await vi.advanceTimersByTimeAsync(2000);
      });
      await act(async () => {
        answer().ok({ status: "retrying", servers: null, reason: REASON });
      });

      // Both halves: the paragraph has it to render, and it was spoken.
      expect(result.current.retrying).toBe(REASON);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(200);
      });
      expect(said()).toContain(REASON);
    } finally {
      vi.useRealTimers();
    }
  });

  it("says it once, not every two seconds", async () => {
    // The poll repeats the same reason on every tick for as long as the condition lasts. A
    // region restating it each time would talk over everything else on the page, so only a
    // CHANGE is spoken -- and this is the assertion that tells the two apart.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const { answer } = pollingHook();

      for (let i = 0; i < 3; i++) {
        await act(async () => {
          await vi.advanceTimersByTimeAsync(2000);
        });
        await act(async () => {
          answer().ok({ status: "retrying", servers: null, reason: REASON });
        });
      }

      // Three identical polls, one sentence. Asserted on the call and not on the regions: they
      // hold one sentence at a time and alternate, so three of the same sentence and one of it
      // leave identical markup, and the assertion could not fail.
      expect(vi.mocked(announce)).toHaveBeenCalledTimes(1);
      expect(vi.mocked(announce)).toHaveBeenCalledWith(REASON);
    } finally {
      vi.useRealTimers();
    }
  });

  it("says the new reason when the poll reports a different one", async () => {
    // The other side of "only on a change": the guard must not turn into "say the first one and
    // then go quiet", which would leave the operator on a stale explanation of the wait.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const { answer } = pollingHook();
      const SECOND = "Reaper is still waiting for your Plex server to finish starting.";

      for (const reason of [REASON, REASON, SECOND]) {
        await act(async () => {
          await vi.advanceTimersByTimeAsync(2000);
        });
        await act(async () => {
          answer().ok({ status: "retrying", servers: null, reason });
        });
      }

      expect(vi.mocked(announce).mock.calls.map(([t]) => t)).toEqual([REASON, SECOND]);
    } finally {
      vi.useRealTimers();
    }
  });
});
