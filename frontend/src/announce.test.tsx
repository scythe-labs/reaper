// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The region the app speaks its successes into (#175). What is pinned here is the four
// properties that make a polite region actually speak: it exists before the message does, the
// same sentence twice is two announcements, a sentence is never blanked before it has had a
// turn, and nothing survives into the next mount.
import { render, screen } from "@testing-library/react";
import { act } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { announce, Announcer, useSlowWait } from "./announce";

/** Both regions, in DOM order. Found by role rather than by class, which is the only way a
 *  test can tell that a reader would reach them at all. */
const regions = () => screen.getAllByRole("status");

/** What the operator would hear: whichever region is currently holding the sentence. */
const spoken = () =>
  regions()
    .map((r) => r.textContent)
    .filter((t) => t !== "")
    .join("|");

/** A component that is waiting for something, standing in for the six loading affordances
 *  `useSlowWait` serves. Takes the sentence as a prop so a test can drive the wait ending
 *  (`null`) as well as the component going away. */
function Waiting({ sentence }: { sentence: string | null }) {
  useSlowWait(sentence);
  return null;
}

/** How long a wait runs before it is worth saying. Written out rather than imported from the
 *  module under test: the contract being pinned is "two seconds", and a test reading the same
 *  constant production reads would hold whatever the constant became (rule 141). */
const SLOW_WAIT_MS = 2000;

// Rule 133: fake timers left standing are inherited by the next test in the file, and by every
// file after it in the same worker.
afterEach(() => {
  vi.useRealTimers();
});

describe("the announcer", () => {
  it("is mounted and empty before anything has been said", () => {
    // The load-bearing property, and the reason this is a module rather than a component
    // rendered next to each message: several readers only watch live regions that were
    // already in the DOM, so a region inserted together with its text reads as correct and
    // stays silent. That is why `Notice` had to reach for `role="alert"` instead.
    render(<Announcer />);

    expect(regions()).toHaveLength(2);
    for (const region of regions()) {
      expect(region).toHaveAttribute("aria-live", "polite");
      expect(region).toHaveAttribute("aria-atomic", "true");
      expect(region).toHaveTextContent("");
    }
  });

  it("puts a message into one of the regions", () => {
    render(<Announcer />);

    act(() => announce("Policy saved."));

    expect(spoken()).toBe("Policy saved.");
  });

  it("says the same sentence twice when it happens twice", () => {
    // The defect this shape exists to avoid: saving twice says "Policy saved." twice, and a
    // text node that does not change is not announced -- so a single region would have made
    // the second save exactly as silent as the bug being fixed. The message moves to the
    // OTHER region, so whichever one receives it has changed and speaks.
    vi.useFakeTimers();
    render(<Announcer />);

    act(() => announce("Policy saved."));
    const firstHolder = regions().findIndex((r) => r.textContent !== "");

    act(() => announce("Policy saved."));
    // The second waits for the first's turn (the queue), then moves regions.
    act(() => void vi.advanceTimersByTime(400));
    const secondHolder = regions().findIndex((r) => r.textContent !== "");

    expect(spoken()).toBe("Policy saved.");
    expect(secondHolder).not.toBe(firstHolder);
  });

  it("leaves only one region holding the message, so it is not read twice", () => {
    render(<Announcer />);

    act(() => announce("Rule added."));

    expect(regions().filter((r) => r.textContent !== "")).toHaveLength(1);
    expect(spoken()).toBe("Rule added.");
  });

  it("says nothing for an empty message", () => {
    render(<Announcer />);

    act(() => announce(""));

    expect(spoken()).toBe("");
  });

  it("gives the first sentence its turn before the second replaces it", () => {
    // The store holds ONE sentence, so a second `announce()` on the same tick used to blank the
    // region holding the first: it existed for one commit, and a reader that had not observed
    // it yet never would. One press of the policy page's Save does exactly this, firing two
    // mutations that each announce; so does a reap, whose stages speak while every other
    // surface in the app is still free to (#193). Both sentences are now said, in order.
    vi.useFakeTimers();
    render(<Announcer />);

    act(() => {
      announce("Movie policy saved.");
      announce("Pace and limits saved.");
    });
    expect(spoken()).toBe("Movie policy saved.");

    act(() => void vi.advanceTimersByTime(400));
    expect(spoken()).toBe("Pace and limits saved.");
    // And still only one region holds it, so it is read once.
    expect(regions().filter((r) => r.textContent !== "")).toHaveLength(1);
  });

  it("drains a whole run of sentences in the order they were said", () => {
    // A reap speaks at each stage while its status polls every second. Nothing may be dropped
    // and nothing may jump the line.
    vi.useFakeTimers();
    render(<Announcer />);

    act(() => {
      announce("Reaping 3 souls.");
      announce("Half done.");
      announce("Reap finished.");
    });

    const heard = [spoken()];
    for (let i = 0; i < 2; i++) {
      act(() => void vi.advanceTimersByTime(400));
      heard.push(spoken());
    }
    expect(heard).toEqual(["Reaping 3 souls.", "Half done.", "Reap finished."]);
  });

  it("falls quiet once the queue is empty, rather than holding the last sentence forever", () => {
    // An atomic region still holding text is not re-read, but leaving it there means the next
    // identical sentence is a text node that did not change -- the silent-second-save bug the
    // two regions exist to avoid, reintroduced by a queue that never lets go.
    vi.useFakeTimers();
    render(<Announcer />);

    act(() => announce("Password saved."));
    act(() => void vi.advanceTimersByTime(400));
    expect(spoken()).toBe("Password saved.");

    act(() => announce("Password saved."));
    expect(spoken()).toBe("Password saved.");
    expect(regions().filter((r) => r.textContent !== "")).toHaveLength(1);
  });

  it("says nothing when nothing is mounted to hear it", () => {
    // Guards the two tests below from proving the wrong thing: they assert silence at 1999 ms,
    // and silence is also what an unmounted region produces. Both mount `Announcer`, so this
    // pins that the mounting is what makes the 2000 ms case different.
    vi.useFakeTimers();
    render(<Waiting sentence="Still loading Reaper." />);

    act(() => void vi.advanceTimersByTime(5000));

    expect(screen.queryAllByRole("status")).toHaveLength(0);
  });

  it("opens silent again after the last region unmounts", () => {
    // Nothing mounted can hear it, so nothing can be said. Without the reset the next mount
    // opens holding the last thing the previous one announced -- an operator signing back in
    // to "Policy saved.", and, in this suite, one test's sentence read as the next test's.
    const first = render(<Announcer />);
    act(() => announce("Password saved."));
    expect(spoken()).toBe("Password saved.");
    first.unmount();

    render(<Announcer />);

    expect(spoken()).toBe("");
  });
});

// The app's loading affordances (#332). Seven of them carried a live region of their own,
// mounted in the same commit as their text -- the shape several readers never announce, which
// is the same bug `ReviewQueue`'s two toasts had and the reason `Notice` reaches for
// `role="alert"`. Six now speak through the region above instead, and only once the wait has
// been a wait: a spinner that announces on every route change is the other failure, and most of
// these are gone in a few hundred milliseconds.
//
// So BOTH halves are pinned in every case below. Asserting only that a long wait speaks would
// pass against a hook that announces on mount, which is the noise this shape exists to avoid.
describe("a wait that runs long", () => {
  it("is silent for as long as it is quick", () => {
    vi.useFakeTimers();
    render(
      <>
        <Announcer />
        <Waiting sentence="Still loading Reaper." />
      </>,
    );

    act(() => void vi.advanceTimersByTime(SLOW_WAIT_MS - 1));

    expect(spoken()).toBe("");
  });

  it("is said out loud once it has run long", () => {
    vi.useFakeTimers();
    render(
      <>
        <Announcer />
        <Waiting sentence="Still loading Reaper." />
      </>,
    );

    act(() => void vi.advanceTimersByTime(SLOW_WAIT_MS));

    expect(spoken()).toBe("Still loading Reaper.");
  });

  it("says nothing when the thing arrives before the wait is up", () => {
    // The common case, and why this is a delay rather than an announcement on mount: a
    // lazily-loaded route lands well inside the window on any ordinary connection.
    vi.useFakeTimers();
    const view = render(
      <>
        <Announcer />
        <Waiting sentence="Still loading Reaper." />
      </>,
    );

    act(() => void vi.advanceTimersByTime(SLOW_WAIT_MS - 1));
    view.rerender(
      <>
        <Announcer />
        <Waiting sentence={null} />
      </>,
    );
    act(() => void vi.advanceTimersByTime(SLOW_WAIT_MS));

    expect(spoken()).toBe("");
  });

  it("says nothing when the component holding the wait goes away first", () => {
    // The other way a wait ends, and the one `RouteLoading` and the two panel fallbacks take:
    // they do not switch to `null`, they unmount. An effect cleanup that canceled only on the
    // prop change would leave those three announcing every fast load.
    vi.useFakeTimers();
    const view = render(
      <>
        <Announcer />
        <Waiting sentence="Still loading Reaper." />
      </>,
    );

    act(() => void vi.advanceTimersByTime(SLOW_WAIT_MS - 1));
    view.rerender(
      <>
        <Announcer />
      </>,
    );
    act(() => void vi.advanceTimersByTime(SLOW_WAIT_MS));

    expect(spoken()).toBe("");
  });

  it("speaks into a region that was already there, which is the point", () => {
    // The property the seven affordances lacked. Their region arrived in the same commit as
    // its text; this one has been in the DOM for the whole wait by the time anything is said.
    vi.useFakeTimers();
    render(
      <>
        <Announcer />
        <Waiting sentence="Still gathering requests." />
      </>,
    );

    expect(regions()).toHaveLength(2);
    for (const region of regions()) expect(region).toHaveTextContent("");

    act(() => void vi.advanceTimersByTime(SLOW_WAIT_MS));

    expect(regions()).toHaveLength(2);
    expect(spoken()).toBe("Still gathering requests.");
  });
});
