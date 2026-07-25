// SPDX-License-Identifier: AGPL-3.0-or-later
// The Back button must step back through the UI (close the topmost open thing, then unwind tab
// changes) and only leave the app once nothing is open. These tests drive the popstate handler
// directly -- dispatching a popstate is exactly what the browser does on a Back press once our
// sentinel is parked -- so the unwinding order is pinned regardless of jsdom's history timing.
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState, type ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { BackNavProvider, useBackGuard, useBackNav, useModalLayer, useModalOpen } from "./backnav";

afterEach(() => {
  // Clear any sentinel left in history.state so the next test's provider mounts clean (its
  // B-12 reconcile keys on exactly that marker).
  history.replaceState(null, "");
});

/** A Back press, once our sentinel is parked, arrives as a popstate. */
function pressBack() {
  act(() => {
    window.dispatchEvent(new PopStateEvent("popstate"));
  });
}

function Overlay() {
  const [open, setOpen] = useState(false);
  useBackGuard(open, () => setOpen(false));
  return (
    <div>
      <button onClick={() => setOpen(true)}>open</button>
      <button onClick={() => setOpen(false)}>close</button>
      {open && <div role="dialog">the overlay</div>}
    </div>
  );
}

function TwoOverlays() {
  const [a, setA] = useState(false);
  const [b, setB] = useState(false);
  useBackGuard(a, () => setA(false));
  useBackGuard(b, () => setB(false));
  return (
    <div>
      <button onClick={() => setA(true)}>openA</button>
      <button onClick={() => setB(true)}>openB</button>
      <button onClick={() => setB(false)}>closeB</button>
      {a && <span>A open</span>}
      {b && <span>B open</span>}
    </div>
  );
}

function GuardedOverlay() {
  const [open, setOpen] = useState(false);
  const [locked, setLocked] = useState(false);
  // The same shape ScheduleModal uses: Back is refused while a save is in flight (locked).
  useBackGuard(
    open,
    () => setOpen(false),
    () => !locked,
  );
  return (
    <div>
      <button onClick={() => setOpen(true)}>open</button>
      <button onClick={() => setLocked(true)}>lock</button>
      <button onClick={() => setLocked(false)}>unlock</button>
      {open && <div role="dialog">the overlay</div>}
    </div>
  );
}

/** A tab change and an overlay in one tree: the pairing that exposed the shared-entry bug. */
function TabsThenOverlay() {
  const [view, setView] = useState("first");
  const [open, setOpen] = useState(false);
  const { pushNav } = useBackNav();
  useBackGuard(open, () => setOpen(false));
  return (
    <div>
      <span>view: {view}</span>
      <button
        onClick={() => {
          pushNav(() => setView("first"));
          setView("second");
        }}
      >
        go second
      </button>
      <button onClick={() => setOpen(true)}>open</button>
      {open && <div role="dialog">the overlay</div>}
    </div>
  );
}

function Tabs() {
  const [view, setView] = useState("first");
  const { pushNav } = useBackNav();
  const go = (next: string) => {
    if (next !== view) pushNav(() => setView(view));
    setView(next);
  };
  return (
    <div>
      <span>view: {view}</span>
      <button onClick={() => go("second")}>go second</button>
      <button onClick={() => go("third")}>go third</button>
    </div>
  );
}

describe("backnav", () => {
  it("Back closes an open overlay instead of leaving", async () => {
    render(
      <BackNavProvider>
        <Overlay />
      </BackNavProvider>,
    );
    await userEvent.click(screen.getByText("open"));
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    pressBack();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("unwinds overlays newest-first, one Back press each", async () => {
    render(
      <BackNavProvider>
        <TwoOverlays />
      </BackNavProvider>,
    );
    await userEvent.click(screen.getByText("openA"));
    await userEvent.click(screen.getByText("openB"));
    expect(screen.getByText("A open")).toBeInTheDocument();
    expect(screen.getByText("B open")).toBeInTheDocument();

    // The one opened last closes first.
    pressBack();
    expect(screen.queryByText("B open")).not.toBeInTheDocument();
    expect(screen.getByText("A open")).toBeInTheDocument();

    pressBack();
    expect(screen.queryByText("A open")).not.toBeInTheDocument();
  });

  it("Back restores the previous tab, one step at a time", async () => {
    render(
      <BackNavProvider>
        <Tabs />
      </BackNavProvider>,
    );
    await userEvent.click(screen.getByText("go second"));
    await userEvent.click(screen.getByText("go third"));
    expect(screen.getByText("view: third")).toBeInTheDocument();

    pressBack();
    expect(screen.getByText("view: second")).toBeInTheDocument();

    pressBack();
    expect(screen.getByText("view: first")).toBeInTheDocument();
  });

  it("Back refuses a guarded overlay while it is locked, then closes it once unlocked", async () => {
    render(
      <BackNavProvider>
        <GuardedOverlay />
      </BackNavProvider>,
    );
    await userEvent.click(screen.getByText("open"));
    await userEvent.click(screen.getByText("lock"));
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    // Locked (a save in flight): Back is refused and the sentinel re-armed, so the overlay stays.
    pressBack();
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    // Unlock and press Back again: the re-armed guard now closes it, not a dead press (B-11).
    await userEvent.click(screen.getByText("unlock"));
    pressBack();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("parks its own history entry per layer, so each Back reveals that layer's own snapshot", async () => {
    // iOS files a back-forward snapshot against each history entry when the page navigates away
    // from it, and paints that snapshot during an edge-swipe back. Sharing one entry across
    // layers meant a card opened after a tab change swiped back to a picture of the list taken at
    // the tab change -- the top of the list -- frozen there for seconds. Opening a layer must
    // push an entry of its own even when one is already parked.
    const pushSpy = vi.spyOn(history, "pushState");
    render(
      <BackNavProvider>
        <TabsThenOverlay />
      </BackNavProvider>,
    );
    await userEvent.click(screen.getByText("go second"));
    expect(pushSpy).toHaveBeenCalledTimes(1);

    await userEvent.click(screen.getByText("open"));
    expect(pushSpy).toHaveBeenCalledTimes(2);

    // Still one Back press per layer, newest-first: the overlay closes, the tab change survives.
    pressBack();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByText("view: second")).toBeInTheDocument();

    // And the tab change kept its own entry, so the next Back unwinds it rather than leaving.
    pressBack();
    expect(screen.getByText("view: first")).toBeInTheDocument();
    pushSpy.mockRestore();
  });

  it("gives a layer's entry back when it closes by its own control, not only the last one", async () => {
    // Each layer owns one entry now, so each non-Back close must hand exactly that one back --
    // otherwise entries pile up and later Back presses are dead.
    const backSpy = vi.spyOn(history, "back").mockImplementation(() => {});
    const { unmount } = render(
      <BackNavProvider>
        <TwoOverlays />
      </BackNavProvider>,
    );
    await userEvent.click(screen.getByText("openA"));
    await userEvent.click(screen.getByText("openB"));
    expect(backSpy).not.toHaveBeenCalled();

    // B closes by its own control while A is still open: under the shared-entry model this
    // un-parked nothing, because a layer remained.
    await userEvent.click(screen.getByText("closeB"));
    expect(backSpy).toHaveBeenCalledTimes(1);

    // Unmount while the mock still stands in for the browser: A is open, so its teardown hands
    // an entry back too, and a real history.back() here would land a stray popstate in whichever
    // test runs next.
    unmount();
    expect(backSpy).toHaveBeenCalledTimes(2);
    backSpy.mockRestore();
  });

  it("reconciles a sentinel left parked before a reload, so the first Back is not dead", () => {
    // Post-reload: the sentinel entry is the current one (its pushState state survived), but the
    // provider's in-memory parkedRef is fresh false. On mount it steps back over the stale entry.
    history.pushState({ __reaperBack: true }, "");
    const backSpy = vi.spyOn(history, "back").mockImplementation(() => {});
    render(
      <BackNavProvider>
        <Overlay />
      </BackNavProvider>,
    );
    expect(backSpy).toHaveBeenCalledTimes(1);
    backSpy.mockRestore();
  });

  it("steps back over exactly one stale sentinel, never chasing the rest", () => {
    // A reload with two layers open leaves two stale entries, and it is tempting to consume them
    // all. It must not: stepping off the reloaded entry crosses a document boundary and loads the
    // page again, which re-runs this reconcile with a fresh bound, walking the tab out of the app
    // one load at a time. The mock stands in for that browser -- each back() reports the popstate
    // that follows -- and one step is the whole contract.
    const stack: (object | null)[] = [null, { __reaperBack: true }, { __reaperBack: true }];
    const backSpy = vi.spyOn(history, "back").mockImplementation(() => {
      stack.pop();
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    const stateSpy = vi
      .spyOn(history, "state", "get")
      .mockImplementation(() => stack[stack.length - 1]);

    render(
      <BackNavProvider>
        <Overlay />
      </BackNavProvider>,
    );
    expect(backSpy).toHaveBeenCalledTimes(1);
    expect(stack).toHaveLength(2);

    stateSpy.mockRestore();
    backSpy.mockRestore();
  });

  it("takes scroll restoration off auto while mounted, and restores it on unmount", () => {
    // With the browser default `auto`, the sentinel this provider parks on open and the
    // history.back() it runs on close both let the engine yank the page to the top (worse
    // with the card list's CSS containment). The provider takes it to `manual` so the reviewer
    // stays put, and restores the prior mode on unmount. jsdom leaves scrollRestoration absent
    // (a real browser always has it); seed it so the guarded path runs, then clear it.
    history.scrollRestoration = "auto";
    const { unmount } = render(
      <BackNavProvider>
        <Overlay />
      </BackNavProvider>,
    );
    expect(history.scrollRestoration).toBe("manual");
    unmount();
    expect(history.scrollRestoration).toBe("auto");
    // Leave history as jsdom hands it to the next test (the property is optional there).
    delete (history as { scrollRestoration?: ScrollRestoration }).scrollRestoration;
  });

  it("a layer closed by its own control is no longer a Back step", async () => {
    render(
      <BackNavProvider>
        <Overlay />
      </BackNavProvider>,
    );
    await userEvent.click(screen.getByText("open"));
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    // Close it the normal way (its X / Escape), not with Back.
    await userEvent.click(screen.getByText("close"));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    // A later Back has nothing of ours to unwind and must not throw or reopen anything.
    pressBack();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});

describe("whether a modal is up", () => {
  // The keyboard handlers that walk a list (↑/↓/j/k in the review queue, Escape in the two
  // side panels) have to stand down while a modal owns the keyboard. They used to answer that
  // by probing the DOM for a `[role="dialog"]` element on every keypress -- markup standing in
  // for state React already owned, so any future overlay that was modal without the attribute,
  // or carried it without being modal, silently gained or lost the keyboard (H-2).
  function Readout() {
    return <span>modal: {useModalOpen() ? "up" : "none"}</span>;
  }

  /** Anything ModalShell wraps. The hook, not the markup, is what declares it. */
  function Modal({ children }: { children?: ReactNode }) {
    useModalLayer();
    return <div>{children}</div>;
  }

  function Harness() {
    const [outer, setOuter] = useState(false);
    const [inner, setInner] = useState(false);
    return (
      <>
        <Readout />
        <button onClick={() => setOuter((v) => !v)}>toggle outer</button>
        <button onClick={() => setInner((v) => !v)}>toggle inner</button>
        {outer && <Modal>{inner && <Modal />}</Modal>}
      </>
    );
  }

  it("is false with nothing open, and true while a modal is mounted", async () => {
    render(
      <BackNavProvider>
        <Harness />
      </BackNavProvider>,
    );
    expect(screen.getByText(/^modal:/)).toHaveTextContent("modal: none");

    await userEvent.click(screen.getByText("toggle outer"));
    expect(screen.getByText(/^modal:/)).toHaveTextContent("modal: up");

    await userEvent.click(screen.getByText("toggle outer"));
    expect(screen.getByText(/^modal:/)).toHaveTextContent("modal: none");
  });

  it("counts modals, so closing a stacked one does not hand the keyboard back early", async () => {
    render(
      <BackNavProvider>
        <Harness />
      </BackNavProvider>,
    );
    await userEvent.click(screen.getByText("toggle outer"));
    await userEvent.click(screen.getByText("toggle inner"));
    expect(screen.getByText(/^modal:/)).toHaveTextContent("modal: up");

    // The stacked one closes; the first is still up and still owns the keyboard.
    await userEvent.click(screen.getByText("toggle inner"));
    expect(screen.getByText(/^modal:/)).toHaveTextContent("modal: up");

    await userEvent.click(screen.getByText("toggle outer"));
    expect(screen.getByText(/^modal:/)).toHaveTextContent("modal: none");
  });

  it("is not moved by an overlay that merely carries dialog markup", async () => {
    // `Overlay` renders role="dialog" and registers with Back, like a menu or a side panel
    // would. Neither makes it modal, and the old probe could not tell the difference.
    render(
      <BackNavProvider>
        <Readout />
        <Overlay />
      </BackNavProvider>,
    );
    await userEvent.click(screen.getByText("open"));
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByText(/^modal:/)).toHaveTextContent("modal: none");
  });
});
