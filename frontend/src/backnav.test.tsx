// SPDX-License-Identifier: AGPL-3.0-or-later
// The Back button must step back through the UI (close the topmost open thing, then unwind tab
// changes) and only leave the app once nothing is open. These tests drive the popstate handler
// directly -- dispatching a popstate is exactly what the browser does on a Back press once our
// sentinel is parked -- so the unwinding order is pinned regardless of jsdom's history timing.
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useRef, useState, type ReactNode, type RefObject } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  BackNavProvider,
  useBackCloseMirror,
  useBackGuard,
  useBackNav,
  useModalLayer,
  useModalOpen,
} from "./backnav";

afterEach(() => {
  // Restores here, at the start of the next test, rather than at the end of this one. A test
  // that fails partway through never reaches its own mockRestore, and a leaked history.state spy
  // would hand the next test a browser that lies, reading as a second failure in code that is
  // fine.
  vi.restoreAllMocks();
  // Clears any sentinel left in history.state so the next test's provider mounts clean. Its
  // reconcile step keys on exactly that marker.
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
  // The same shape ScheduleModal uses. Back is refused while a save is in flight (locked).
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

/** The split the three real modals use, which `GuardedOverlay` above cannot show. The parent
 *  owns the Back registration and the ref, and the modal inside it mirrors its whole `canClose`
 *  into that ref. No modal here can hold the lock in the parent directly, because the reason to
 *  stay open is the modal's own mutation state. */
function MirroredModal({
  blockCloseRef,
  onClose,
}: {
  blockCloseRef: RefObject<boolean>;
  onClose: () => void;
}) {
  const [saving, setSaving] = useState(false);
  useBackCloseMirror(blockCloseRef, !saving);
  return (
    <div role="dialog">
      <button onClick={() => setSaving(true)}>start saving</button>
      <button onClick={onClose}>close it</button>
    </div>
  );
}

function MirroredOverlay() {
  const [open, setOpen] = useState(false);
  const blockCloseRef = useRef(false);
  useBackGuard(
    open,
    () => setOpen(false),
    () => !blockCloseRef.current,
  );
  return (
    <div>
      <button onClick={() => setOpen(true)}>open</button>
      {open && <MirroredModal blockCloseRef={blockCloseRef} onClose={() => setOpen(false)} />}
    </div>
  );
}

/** Two overlays sharing one piece of state, so opening the second closes the first in a single
 *  commit. This swap is the case that can lose a history entry to its own queued step. */
function SwapOverlays() {
  const [which, setWhich] = useState<"a" | "b" | null>(null);
  useBackGuard(which === "a", () => setWhich(null));
  useBackGuard(which === "b", () => setWhich(null));
  return (
    <div>
      <button onClick={() => setWhich("a")}>openA</button>
      <button onClick={() => setWhich("b")}>swapToB</button>
      {which && <div role="dialog">{which} open</div>}
    </div>
  );
}

/** A tab change and an overlay in one tree, the pairing that stresses the shared-entry logic. */
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

    // Locked (a save in flight). Back is refused and the sentinel re-armed, so the overlay stays.
    pressBack();
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    // Unlock and press Back again. The re-armed guard now closes it, rather than a dead press.
    await userEvent.click(screen.getByText("unlock"));
    pressBack();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("closes on Back until the modal says otherwise, and refuses once it does", async () => {
    // The child half of the guard (`useBackCloseMirror`). The parent registers Back and reads
    // nothing but the ref, so a modal whose reason to stay open is its own mutation state has to
    // put the WHOLE predicate there.
    //
    // Both directions are driven in one pass. A hook that wrote a constant would pass either
    // half alone, and one of the two constants means the guard always refuses to close, which
    // is the trap this test is meant to catch.
    render(
      <BackNavProvider>
        <MirroredOverlay />
      </BackNavProvider>,
    );
    await userEvent.click(screen.getByText("open"));

    pressBack();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    await userEvent.click(screen.getByText("open"));
    await userEvent.click(screen.getByText("start saving"));

    pressBack();
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    // Refused, not spent. The sentinel is re-parked, so the modal's own control still closes it.
    await userEvent.click(screen.getByText("close it"));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("clears the ref when a modal unmounts still refusing", async () => {
    // Read at the ref rather than through Back, because no caller can observe it there. All
    // three parents arm the guard on the same state that mounts the modal, so the guard is gone
    // in the same commit, and the next open writes its own value before any press lands. This
    // pins the hook's contract rather than a failure a user could reach.
    const ref = { current: false };
    const { unmount } = render(<MirroredModal blockCloseRef={ref} onClose={() => {}} />);
    expect(ref.current).toBe(false);

    await userEvent.click(screen.getByText("start saving"));
    expect(ref.current).toBe(true);

    unmount();

    expect(ref.current).toBe(false);
  });

  it("parks its own history entry per layer, so each Back reveals that layer's own snapshot", async () => {
    // iOS records a back-forward snapshot against each history entry when the page navigates
    // away from it, and replays that snapshot during an edge-swipe back. Sharing one entry
    // across layers would show a card opened after a tab change a frozen picture of the list as
    // it looked at the tab change, for a few seconds, instead of the card. Opening a layer must
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

    // Still one Back press per layer, newest first. The overlay closes, and the tab change
    // survives.
    pressBack();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByText("view: second")).toBeInTheDocument();

    // And the tab change kept its own entry, so the next Back unwinds it rather than leaving.
    pressBack();
    expect(screen.getByText("view: first")).toBeInTheDocument();
    pushSpy.mockRestore();
  });

  it("gives a layer's entry back when it closes by its own control, not only the last one", async () => {
    // Each layer owns one entry, so each non-Back close must hand exactly that one back.
    // Otherwise entries pile up and later Back presses are dead.
    const backSpy = vi.spyOn(history, "back").mockImplementation(() => {});
    const { unmount } = render(
      <BackNavProvider>
        <TwoOverlays />
      </BackNavProvider>,
    );
    await userEvent.click(screen.getByText("openA"));
    await userEvent.click(screen.getByText("openB"));
    expect(backSpy).not.toHaveBeenCalled();

    // B closes by its own control while A is still open. This must not un-park an entry, since
    // a layer still remains.
    await userEvent.click(screen.getByText("closeB"));
    expect(backSpy).toHaveBeenCalledTimes(1);

    // Unmounts while the mock still stands in for the browser. A is open, so its teardown hands
    // an entry back too, and a real history.back() here would land a stray popstate in whichever
    // test runs next. The step is deferred to the end of the tick (see `unpark`), so this waits
    // one microtask for it rather than reading the count straight after the unmount.
    unmount();
    await Promise.resolve();
    expect(backSpy).toHaveBeenCalledTimes(2);
    backSpy.mockRestore();
  });

  it("lets a layer opening in the same tick take over the entry of one closing", async () => {
    // React runs every layout-effect cleanup before any setup, so a swap (one overlay closing as
    // another opens) calls remove() first and register() second. Issuing the history.back() call
    // inline would then land it before the pushState in the same tick, and a browser resolves
    // that traversal against the entry that was current when back() was called. The entry just
    // pushed would be discarded, ending a step lower than the count expects. The next close
    // would then step off an entry that was never parked and leave the overlay still open.
    // Nothing should move here. The opening layer takes over the entry the closing one had not
    // handed back yet.
    const pushSpy = vi.spyOn(history, "pushState");
    const backSpy = vi.spyOn(history, "back").mockImplementation(() => {});
    render(
      <BackNavProvider>
        <SwapOverlays />
      </BackNavProvider>,
    );
    await userEvent.click(screen.getByText("openA"));
    expect(pushSpy).toHaveBeenCalledTimes(1);

    await userEvent.click(screen.getByText("swapToB"));
    expect(screen.getByRole("dialog")).toHaveTextContent("b open");
    expect(backSpy).not.toHaveBeenCalled();
    expect(pushSpy).toHaveBeenCalledTimes(1);

    // And the one entry still unwinds the one open layer.
    pressBack();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    backSpy.mockRestore();
    pushSpy.mockRestore();
  });

  it("reconciles a sentinel left parked before a reload, so the first Back is not dead", () => {
    // After a reload, the sentinel entry is the current one, since its pushState state
    // survived, but the provider's in-memory count is fresh. On mount it steps back over the
    // stale entry.
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

  it("walks off every stale sentinel, and stops at the first entry it did not park", () => {
    // A reload with two layers open leaves two stale entries. Stepping over only the first
    // would leave the second as a dead Back press, so this reconcile has to walk both. Driving
    // the same shape in a real browser settled how far it is safe to walk: entries parked with
    // pushState stay same-document with the reloaded page, so each step is a popstate and no
    // step reloads anything. The walk continues one settled step at a time, and stops on the
    // entry whose state is not ours, which is the app's own first entry.
    history.pushState({ __reaperBack: true }, ""); // so history.length > 1 whatever ran before
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
    expect(backSpy).toHaveBeenCalledTimes(2);
    // Standing on the entry it did not park, having left it alone.
    expect(stack).toEqual([null]);

    stateSpy.mockRestore();
    backSpy.mockRestore();
  });

  it("never steps off an entry it did not park, however far its own count has drifted", async () => {
    // A long-press on Back jumps several entries and reports a single popstate, so this
    // provider's count can run ahead of the actual stack. Closing a layer would then owe an
    // entry the browser does not have, and taking that step would navigate out of Reaper with a
    // panel still open. The browser is asked first, since `history.state` carries the app's own
    // marker, so a drift costs nothing instead.
    history.replaceState(null, ""); // an entry that is not ours, under an open layer
    const backSpy = vi.spyOn(history, "back").mockImplementation(() => {});
    const pushSpy = vi.spyOn(history, "pushState").mockImplementation(() => {});
    render(
      <BackNavProvider>
        <Overlay />
      </BackNavProvider>,
    );
    await userEvent.click(screen.getByText("open"));
    await userEvent.click(screen.getByText("close"));
    await Promise.resolve();
    expect(backSpy).not.toHaveBeenCalled();
    backSpy.mockRestore();
    pushSpy.mockRestore();
  });

  it("takes scroll restoration off auto while mounted, and restores it on unmount", () => {
    // With the browser default `auto`, the sentinel this provider parks on open and the
    // history.back() it runs on close both let the engine yank the page to the top, worse
    // still with the card list's CSS containment. The provider sets it to `manual` so the
    // reviewer stays put, and restores the prior mode on unmount. jsdom leaves scrollRestoration
    // absent, unlike a real browser, so this seeds it to exercise the guarded path, then clears
    // it.
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
  // The keyboard handlers that walk a list (up/down/j/k in the review queue, Escape in the two
  // side panels) have to stand down while a modal owns the keyboard. `useModalOpen` answers that
  // from React state rather than by probing the DOM for a `[role="dialog"]` element, so a future
  // overlay that is modal without the attribute, or carries it without being modal, cannot
  // silently gain or lose the keyboard.
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
    // would. Neither makes it modal.
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
