// SPDX-License-Identifier: AGPL-3.0-or-later
// The Back button must step back through the UI (close the topmost open thing, then unwind tab
// changes) and only leave the app once nothing is open. These tests drive the popstate handler
// directly -- dispatching a popstate is exactly what the browser does on a Back press once our
// sentinel is parked -- so the unwinding order is pinned regardless of jsdom's history timing.
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { BackNavProvider, useBackGuard, useBackNav } from "./backnav";

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
