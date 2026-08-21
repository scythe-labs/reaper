// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The PIN poll's run guard. Stopping the timer never stopped a request already in the air,
// so an answer that landed after the sign-in had settled still called the handlers: the
// operator watched "Plex sign-in failed" paint over a session that had just succeeded, or
// got signed in a moment after pressing Cancel (B-11).
//
// These drive the hook directly. Both cases need two polls in flight at once with control
// over which settles first, which is not something a rendered panel lets you arrange.

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { act, render, renderHook, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { announce, Announcer } from "../announce";
import type { PinPollResult } from "./PlexPin";
import { CHOOSE_SERVER_SAID, usePlexPinPoll } from "./PlexPin";

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

// The other transition this flow reaches on a timer: the account owns several servers, so the
// wait is replaced by a picker. Both callers announced it, in their own words, from their own
// handlers -- and the pair had already been out of step once, with Settings speaking and the
// login screen silent (#177, rule 72). One declaration in the hook is what makes them agree by
// construction rather than by whoever edits one of them next remembering the other (rule 144).
describe("the account that owns several servers", () => {
  beforeEach(() => {
    vi.mocked(announce).mockClear();
  });

  const said = () =>
    screen
      .getAllByRole("status")
      .map((n) => n.textContent)
      .filter((t) => t !== "");

  it("is announced for a caller that passes no handler at all", async () => {
    // Deliberately no `onChooseServer`. A caller cannot reach the picker without the sentence,
    // which is why it moved off the two handlers that used to carry it.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const { poll, answer } = deferredPolls();
      render(<Announcer />);
      const { result } = renderHook(() =>
        usePlexPinPoll({ poll, onOk: vi.fn(), onFailed: vi.fn(), onTimedOut: vi.fn() }),
      );
      act(() => result.current.begin(42));

      await act(async () => {
        await vi.advanceTimersByTimeAsync(2000);
      });
      await act(async () => {
        answer().ok({
          status: "choose_server",
          servers: [{ name: "Server", machine_identifier: "m1" }],
        });
      });

      // Both halves, the way the reason above is checked: the list is held for the picker to
      // render, and the sentence reached a real region.
      expect(result.current.servers).toHaveLength(1);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(200);
      });
      expect(said()).toContain(CHOOSE_SERVER_SAID);
    } finally {
      vi.useRealTimers();
    }
  });
});

// Rule 144, as a guard rather than a comment asking the next author to remember. The hook says
// this sentence for every caller now, so a caller that ALSO says it makes the operator hear it
// twice -- and each file's own tests stay green either way, because neither can see the other
// one speaking. This is what one declaration is worth, so it fails by name in the file that
// broke it.
//
// Bounded, per rule 147: a substring match reads the sentence however it is spelled around it --
// a bare literal, a template, a constant re-declared by hand -- but only while it is spelled out.
// A caller composing it from pieces would pass, which is why the anchor asserts the declaration
// is still where the two checks below think it is, rather than letting the walk quietly empty.
describe("who is allowed to say the picker sentence", () => {
  const read = (name: string) =>
    readFileSync(join(dirname(fileURLToPath(import.meta.url)), name), "utf8");

  it("is this hook, and neither of the two screens driving it", () => {
    expect(read("PlexPin.tsx")).toContain(`= i18next.t("plex.pin.chooseServerSaid")`);

    for (const caller of ["Login.tsx", "PlexPanel.tsx"]) {
      expect(
        read(caller).includes(CHOOSE_SERVER_SAID),
        `${caller} states the picker sentence itself. The hook already announces it for every ` +
          `caller, so this one says it twice. Delete the local copy (#177, rules 72 and 144).`,
      ).toBe(false);
    }
  });
});
