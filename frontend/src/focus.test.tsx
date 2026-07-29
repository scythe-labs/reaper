// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Where focus goes when a control removes the row it lives in (#173). Activating such a control
// destroys the focused element, so focus falls to `<body>` and the next Tab restarts at the top
// of the document -- on the policy page, a ~1,900-line form. An operator removing three tags was
// thrown to the top three times.
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useRef, useState } from "react";
import { describe, expect, it } from "vitest";
import { REMOVES_ITS_ROW, useRemovalFocus, useSavebarFocus } from "./focus";

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

    // The NEXT row, which is what the eye expects after a delete: "c" has slid up into b's slot.
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
    // The effect runs on every commit, so it has to be inert unless `removing` armed it --
    // otherwise an unrelated re-render would yank the operator into the list.
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
    // the control that was activated. Focus fell to `<body>` and the next Tab restarted above the
    // whole form -- the policy page's is ~1,900 lines (#173).
    const user = userEvent.setup();
    render(<Panel />);
    await user.type(screen.getByRole("textbox", { name: "A field" }), "x");

    await user.click(screen.getByRole("button", { name: "Discard" }));

    expect(screen.getByRole("heading", { name: "General" })).toHaveFocus();
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
