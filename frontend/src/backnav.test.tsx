// SPDX-License-Identifier: AGPL-3.0-or-later
// The Back button must step back through the UI (close the topmost open thing, then unwind tab
// changes) and only leave the app once nothing is open. These tests drive the popstate handler
// directly -- dispatching a popstate is exactly what the browser does on a Back press once our
// sentinel is parked -- so the unwinding order is pinned regardless of jsdom's history timing.
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it } from "vitest";
import { BackNavProvider, useBackGuard, useBackNav } from "./backnav";

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
