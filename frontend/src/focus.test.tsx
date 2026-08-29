// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Where focus goes when a control removes the row it lives in. Activating such a control
// destroys the focused element, so without a fix, focus falls to `<body>` and the next Tab
// restarts at the top of the document, which on the policy page is a form of about 1,900
// lines. Removing three tags in a row would throw the operator to the top three times.
import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useRef, useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { REMOVES_ITS_ROW, useRemovalFocus, useSavebarFocus, useSuccessorFocus } from "./focus";

function List({ start }: { start: string[] }) {
  const [items, setItems] = useState(start);
  const addRef = useRef<HTMLInputElement>(null);
  const rows = useRemovalFocus(addRef);
  return (
    <>
      <button>Something before the list</button>
      <div ref={rows.ref as React.RefObject<HTMLDivElement>}>
        {items.map((it, i) => (
          <span key={it}>
            {it}
            <button
              {...REMOVES_ITS_ROW}
              aria-label={`Remove ${it}`}
              onClick={() => {
                rows.removing(i);
                setItems((prev) => prev.filter((x) => x !== it));
              }}
            >
              ×
            </button>
          </span>
        ))}
      </div>
      <input ref={addRef} aria-label="Add one" />
    </>
  );
}

describe("useRemovalFocus", () => {
  it("moves to the row that took the removed row's place", async () => {
    const user = userEvent.setup();
    render(<List start={["a", "b", "c"]} />);

    await user.click(screen.getByRole("button", { name: "Remove b" }));

    // The next row, which is what the eye expects after a delete: "c" has slid up into b's slot.
    expect(screen.getByRole("button", { name: "Remove c" })).toHaveFocus();
  });

  it("clamps to the last row when the last one is what went", async () => {
    const user = userEvent.setup();
    render(<List start={["a", "b", "c"]} />);

    await user.click(screen.getByRole("button", { name: "Remove c" }));

    expect(screen.getByRole("button", { name: "Remove b" })).toHaveFocus();
  });

  it("falls back to the add box when the list empties", async () => {
    // Nowhere left to go in the list, and the only thing still to do there is add one.
    const user = userEvent.setup();
    render(<List start={["a"]} />);

    await user.click(screen.getByRole("button", { name: "Remove a" }));

    expect(screen.getByRole("textbox", { name: "Add one" })).toHaveFocus();
  });

  it("never leaves focus on the body, which is the whole defect", async () => {
    const user = userEvent.setup();
    render(<List start={["a", "b"]} />);

    await user.click(screen.getByRole("button", { name: "Remove a" }));
    expect(document.activeElement).not.toBe(document.body);
    await user.click(screen.getByRole("button", { name: "Remove b" }));
    expect(document.activeElement).not.toBe(document.body);
  });

  it("leaves focus alone when nothing said a row was going", async () => {
    // The effect runs on every commit, so it has to stay inert unless `removing` armed it.
    // Otherwise an unrelated re-render would yank the operator into the list.
    const user = userEvent.setup();
    render(<List start={["a", "b"]} />);

    const outside = screen.getByRole("button", { name: "Something before the list" });
    await user.click(outside);
    await user.type(screen.getByRole("textbox", { name: "Add one" }), "x");
    outside.focus();

    expect(outside).toHaveFocus();
  });
});

function Panel() {
  const [dirty, setDirty] = useState(false);
  const bar = useSavebarFocus();
  return (
    <>
      <h2 ref={bar.ref as React.RefObject<HTMLHeadingElement>} tabIndex={-1}>
        General
      </h2>
      <input aria-label="A field" onChange={() => setDirty(true)} />
      {dirty && (
        <div className="savebar">
          <button
            onClick={() => {
              bar.leaving();
              setDirty(false);
            }}
          >
            Discard
          </button>
        </div>
      )}
    </>
  );
}

describe("useSavebarFocus", () => {
  it("puts the operator on the panel's heading when the bar takes their button away", async () => {
    // A savebar exists only while something is unsaved, so the press that dismisses it destroys
    // the control that was activated. Without a fix, focus falls to `<body>` and the next Tab
    // restarts above the whole form, which on the policy page is about 1,900 lines.
    const user = userEvent.setup();
    render(<Panel />);
    await user.type(screen.getByRole("textbox", { name: "A field" }), "x");

    await user.click(screen.getByRole("button", { name: "Discard" }));

    expect(screen.getByRole("heading", { name: "General" })).toHaveFocus();
  });

  it("does not drag the page to the heading it focuses", async () => {
    // The landing point is a fixed spot near the top of a form that runs to about 1,900 lines,
    // not somewhere near the operator, so focusing it would scroll Save and Discard back to the
    // top, however far down the operator had gone to make the edit just saved. jsdom computes no
    // layout and cannot be scrolled, so the contract is read off the call instead: focus still
    // moves, and it moves without taking the viewport with it.
    const user = userEvent.setup();
    const focused = vi.spyOn(HTMLHeadingElement.prototype, "focus");
    render(<Panel />);
    await user.type(screen.getByRole("textbox", { name: "A field" }), "x");

    await user.click(screen.getByRole("button", { name: "Discard" }));

    expect(focused).toHaveBeenCalledWith({ preventScroll: true });
    focused.mockRestore();
  });

  it("leaves focus alone while the bar is still up", async () => {
    // A save keeps the bar up while the write is in flight. Moving focus then would take the
    // operator off a Discard they can still press.
    const user = userEvent.setup();
    render(<Panel />);
    const field = screen.getByRole("textbox", { name: "A field" });
    await user.type(field, "x");

    expect(field).toHaveFocus();
    expect(screen.getByRole("heading", { name: "General" })).not.toHaveFocus();
  });
});

/** A control that acts, is disabled while the write is in flight, goes away with what it acted
 *  on, and is replaced by something else. This is the shape of every site `useSuccessorFocus`
 *  covers.
 *
 *  `settle` is what makes one harness cover all three real timings. 0 is a successor that mounts
 *  in the same commit as the press (Plex's Manual address row). Any positive number is one that
 *  waits for an invalidated query to come back (the API key row, the Discord row, the restore
 *  card), and on that path the press paints `disabled` first, which drops focus to `<body>`
 *  before the unmount. `blockFor` adds the third case: a successor that mounts before it is
 *  actable and only then becomes pressable, which two of the sites pass through. `spelling`
 *  drives both forms the "not actable" matcher accepts, since a matcher is proven only against
 *  the spellings it is actually run on. */
function Removable({
  settle = 0,
  blockFor = 0,
  spelling = "disabled",
}: {
  settle?: number;
  blockFor?: number;
  spelling?: "disabled" | "aria-disabled";
}) {
  const [flight, setFlight] = useState<"idle" | "sending" | "done">("idle");
  const [blocked, setBlocked] = useState(blockFor > 0);
  const after = useSuccessorFocus();
  const press = () => {
    after.arriving();
    setFlight("sending");
    if (settle === 0) setFlight("done");
    else setTimeout(() => setFlight("done"), settle);
    if (blockFor > 0) setTimeout(() => setBlocked(false), blockFor);
  };
  return (
    <>
      <button>Something before it</button>
      {flight !== "done" && (
        <button disabled={flight === "sending"} onClick={press}>
          Remove it
        </button>
      )}
      {flight === "done" && (
        <button
          ref={after.ref as React.RefObject<HTMLButtonElement>}
          disabled={spelling === "disabled" ? blocked : undefined}
          aria-disabled={spelling === "aria-disabled" && blocked ? true : undefined}
        >
          What is left to do
        </button>
      )}
    </>
  );
}

describe("useSuccessorFocus", () => {
  const successor = () => screen.getByRole("button", { name: "What is left to do" });
  const pressRemove = () => {
    const btn = screen.getByRole("button", { name: "Remove it" });
    btn.focus();
    fireEvent.click(btn);
  };

  it("lands on a successor that mounts in the same commit as the press", async () => {
    const user = userEvent.setup();
    render(<Removable />);

    await user.click(screen.getByRole("button", { name: "Remove it" }));

    expect(successor()).toHaveFocus();
  });

  it("never leaves focus on the body, which is the whole defect", async () => {
    const user = userEvent.setup();
    render(<Removable />);

    await user.click(screen.getByRole("button", { name: "Remove it" }));

    expect(document.activeElement).not.toBe(document.body);
  });

  it("waits out a round trip and lands on the successor when it finally arrives", async () => {
    // The case `useRemovalFocus` cannot cover: it resolves its target on the very next commit,
    // but on these sites there is no target yet then, so every commit afterwards gets a turn
    // instead.
    vi.useFakeTimers();
    try {
      render(<Removable settle={50} />);
      pressRemove();

      // The press disabled the control it was on, so there is nowhere to stand and nothing to go
      // to yet. This is asserted as "not actable" rather than as `<body>`, because a real
      // browser blurs a control that becomes disabled while jsdom does not, and the hook treats
      // both as lost for exactly that reason. Pinning `<body>` here would pin the jsdom behavior
      // instead of the rule.
      expect(screen.getByRole("button", { name: "Remove it" })).toBeDisabled();
      expect(document.activeElement).toBeDisabled();

      await act(async () => {
        await vi.advanceTimersByTimeAsync(50);
      });

      expect(successor()).toHaveFocus();
    } finally {
      vi.useRealTimers();
    }
  });

  // Focusing a disabled control is a silent no-op: the browser drops it straight back to
  // `<body>`, so a move that "succeeded" would leave the operator exactly where they started.
  // Two of the sites pass through a state whose only candidate is still `disabled`: the restore
  // card's "Cancel restore" button mounts a whole commit before `busy` clears.
  //
  // What this pins is that the request survives that state, not that nothing is focused during
  // it. jsdom refuses `.focus()` on a disabled node by itself, so an assertion that the disabled
  // successor is unfocused would hold even with the guard deleted, and would prove nothing.
  // Deleting the guard spends the request on a no-op, so the successor never gets focus at all,
  // which is what these two tests would fail on.
  for (const spelling of ["disabled", "aria-disabled"] as const) {
    it(`keeps waiting through a successor blocked by ${spelling}, then lands on it`, async () => {
      vi.useFakeTimers();
      try {
        render(<Removable settle={10} blockFor={50} spelling={spelling} />);
        pressRemove();

        await act(async () => {
          await vi.advanceTimersByTimeAsync(10);
        });
        // Mounted, not yet actable, so nobody has been moved onto it.
        expect(successor()).not.toHaveFocus();

        await act(async () => {
          await vi.advanceTimersByTimeAsync(40);
        });

        expect(successor()).toHaveFocus();
      } finally {
        vi.useRealTimers();
      }
    });
  }

  it("leaves focus alone when the operator has moved it themselves", async () => {
    // The difference between a recovery and a steal. A write settling under someone must not pull
    // them out of the control they have since moved to.
    vi.useFakeTimers();
    try {
      render(<Removable settle={50} />);
      pressRemove();
      const elsewhere = screen.getByRole("button", { name: "Something before it" });
      elsewhere.focus();

      await act(async () => {
        await vi.advanceTimersByTimeAsync(50);
      });

      expect(elsewhere).toHaveFocus();
      expect(successor()).not.toHaveFocus();
    } finally {
      vi.useRealTimers();
    }
  });

  it("leaves focus alone when nothing said a successor was coming", async () => {
    // The request is explicit: a component that merely renders the target must move nobody.
    render(<Removable />);
    const elsewhere = screen.getByRole("button", { name: "Something before it" });
    elsewhere.focus();

    expect(elsewhere).toHaveFocus();
    expect(screen.queryByRole("button", { name: "What is left to do" })).toBeNull();
  });
});

/** The other real shape, and the one that decides how broad "lost" has to be: a row whose Save
 *  exists only while it is dirty, whose box is on screen the whole time, and whose press disables
 *  the Save without unmounting it. Plex's web-address row is this shape: it holds the button up
 *  until the `["plex"]` refetch lands.
 *
 *  Here the successor is actable on the very commit after the press, while the pressed control is
 *  still mounted and merely disabled. So the cursor is not on `<body>`, it is parked on a control
 *  nobody can act on. A guard that only checks for `<body>` would decline to move at all, leaving
 *  this row alone unfixed. */
function DirtyRow() {
  const [dirty, setDirty] = useState(false);
  const [sending, setSending] = useState(false);
  const after = useSuccessorFocus();
  return (
    <>
      <button>Something before it</button>
      <input
        ref={after.ref as React.RefObject<HTMLInputElement>}
        aria-label="The address"
        onChange={() => setDirty(true)}
      />
      {dirty && (
        <button
          disabled={sending}
          onClick={() => {
            after.arriving();
            setSending(true);
          }}
        >
          Save
        </button>
      )}
    </>
  );
}

describe("useSuccessorFocus, when the pressed control is disabled but still on screen", () => {
  it("hands focus back to the box, because a disabled control is nowhere to stand", async () => {
    const user = userEvent.setup();
    render(<DirtyRow />);
    const box = screen.getByRole("textbox", { name: "The address" });
    await user.type(box, "x");

    await user.click(screen.getByRole("button", { name: "Save" }));

    // Still there, still unactable, which is the state a `<body>`-only check reads as "fine".
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    expect(box).toHaveFocus();
  });
});
