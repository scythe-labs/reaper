// SPDX-License-Identifier: AGPL-3.0-or-later
// The Spare button's own copy. One rule carries all of it: undecided, the button says what a
// press WILL do (the operator's default length); spared, it says what is in force on THIS item.
// The bug that motivated it: a 90-day spare under a Forever default left the button reading
// "∞ Spared" -- the wrong glyph, and no sign of when the spare ends.
import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useEffect, useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { testQueryClient } from "../test/queryClient";
import { OverrideControls, OverrideMark } from "./OverrideControls";
import { QueueSettingsContext, type QueueSettings } from "./queueSettings";

vi.mock("../api", () => ({ api: { general: vi.fn(), profile: vi.fn() } }));

/** An ISO instant `days` from now. Negative is a spare whose count has already run down. */
function inDays(days: number): string {
  return new Date(Date.now() + days * 86_400_000).toISOString();
}

function draw(props: Partial<Parameters<typeof OverrideControls>[0]> = {}, defaultSpareDays = 0) {
  const shared: QueueSettings = {
    defaultSpareDays,
    unmeasured: { holdsBack: true, isPending: false, isError: false },
  };
  return render(
    <QueryClientProvider client={testQueryClient()}>
      <QueueSettingsContext.Provider value={shared}>
        <OverrideControls
          override={null}
          onSet={vi.fn()}
          onClear={vi.fn()}
          pending={false}
          {...props}
        />
      </QueueSettingsContext.Provider>
    </QueryClientProvider>,
  );
}

/** The ∞ the forever spare wears. Both glyphs are aria-hidden, so they never reach a name. */
const forever = (c: HTMLElement) => c.querySelector(".infinity") !== null;

/** `draw` above passes a constant `pending`, which is the one thing no real caller does: every
 *  one of them threads it from the override mutation (`ReviewQueue`, `WhyPanel`, `ShowPanel`), so
 *  a press that sets a spare disables the control it came from IN THE SAME COMMIT, then settles a
 *  moment later. That is the whole shape of the bug the focus return has to survive, and a fixed
 *  `pending={false}` hides it -- the menu's exits all look like they restore focus. */
function DrawLive({ defaultSpareDays = 30, override = null }: DrawLiveProps) {
  const [pending, setPending] = useState(false);
  useEffect(() => {
    if (!pending) return;
    const id = setTimeout(() => setPending(false), 0);
    return () => clearTimeout(id);
  }, [pending]);
  const shared: QueueSettings = {
    defaultSpareDays,
    unmeasured: { holdsBack: true, isPending: false, isError: false },
  };
  return (
    <QueryClientProvider client={testQueryClient()}>
      <QueueSettingsContext.Provider value={shared}>
        <OverrideControls
          override={override}
          spareExpiresAt={null}
          onSet={() => setPending(true)}
          onClear={() => setPending(true)}
          pending={pending}
        />
      </QueueSettingsContext.Provider>
    </QueryClientProvider>
  );
}
type DrawLiveProps = { defaultSpareDays?: number; override?: "spare" | "reap" | null };

const CARET = "Choose how long to keep it";

describe("the Spare button, undecided", () => {
  it("says what a press will do: ∞ and 'Spare' under a forever default", () => {
    const { container } = draw({}, 0);
    expect(screen.getByRole("button", { name: "Spare" })).toBeTruthy();
    expect(forever(container)).toBe(true);
  });

  it("wears the clock when the default is a set number of days", () => {
    const { container } = draw({}, 30);
    expect(screen.getByRole("button", { name: "Spare" })).toBeTruthy();
    expect(forever(container)).toBe(false);
  });
});

describe("the Spare button, spared", () => {
  it("keeps ∞ and the plain word for a forever spare", () => {
    const { container } = draw({ override: "spare", spareExpiresAt: null });
    expect(screen.getByRole("button", { name: "Spared" })).toBeTruthy();
    expect(forever(container)).toBe(true);
  });

  it("counts down in the panel footer: 'Spared 87d'", () => {
    draw({ override: "spare", spareExpiresAt: inDays(87), roomy: true });
    expect(screen.getByRole("button", { name: "Spared 87d" })).toBeTruthy();
  });

  it("shows the bare count on the fixed narrow tracks, named in full for a screen reader", () => {
    // The visible label is just "87d" (rule 51 leaves no room for the word), so the accessible
    // name carries the rest -- with the visible text still inside it (WCAG 2.5.3).
    const { container } = draw({ override: "spare", spareExpiresAt: inDays(87) });
    const btn = screen.getByRole("button", { name: "Spared 87d left" });
    expect(btn.textContent).toContain("87d");
    expect(btn.textContent).not.toContain("Spared");
    expect(forever(container)).toBe(false);
  });

  it("describes the ITEM's spare, not the default a press would have applied", () => {
    // The original bug: default Forever + a timed spare rendered "∞ Spared". The glyph must
    // follow the spare in force, so a timed spare wears the clock whatever the default is.
    const { container } = draw({ override: "spare", spareExpiresAt: inDays(87) }, 0);
    expect(forever(container)).toBe(false);
  });

  it("puts the end date in the tooltip, which the button has no room to show", () => {
    draw({ override: "spare", spareExpiresAt: inDays(87), roomy: true });
    const btn = screen.getByRole("button", { name: "Spared 87d" });
    expect(btn.getAttribute("title")).toMatch(
      /^Kept until .+\. Click to let Reaper judge it again$/,
    );
  });
});

describe("a spare whose clock has passed", () => {
  // The one state where this control stops being a toggle. The other two answer "is this
  // spared" and press to undo themselves; a spent spare has nothing left to undo, and what the
  // operator opened the row to do is keep the item again. So it offers a fresh spare, and
  // clearing the spent row moves into the length menu, which has room to say what it does.
  const SPENT = { override: "spare", spareExpiresAt: inDays(-3) } as const;
  const SPENT_NAME = "Spare again, the last one expired";

  it("offers a fresh spare instead of resting as a pressed decision", () => {
    const { container } = draw(SPENT);
    const btn = screen.getByRole("button", { name: SPENT_NAME });
    // Dashed, so the row still says a spare ran out right here...
    expect(btn.className).toContain("expired");
    // ...but not the solid fill or the pressed state, which mean "your decision, and it holds".
    expect(btn.className).not.toContain("active");
    expect(btn.getAttribute("aria-pressed")).toBe("false");
    // Not ∞ either: it was a timed spare, and saying "forever" would be the other lie.
    expect(forever(container)).toBe(false);
  });

  it("presses through to a new spare, never to clearing the spent one", async () => {
    // The mis-affordance this replaced: the green button on a spent spare CLEARED it, so the
    // one obvious press did the opposite of what the operator came for.
    const onSet = vi.fn();
    const onClear = vi.fn();
    draw({ ...SPENT, onSet, onClear }, 30);
    await userEvent.setup().click(screen.getByRole("button", { name: SPENT_NAME }));
    expect(onSet).toHaveBeenCalledWith("spare", 30);
    expect(onClear).not.toHaveBeenCalled();
  });

  it("shows the plain word, and names what a press does rather than a status", () => {
    // "0d" is not a smaller amount of "27d" -- it is none of it, and in a pressed green button
    // it read as an active decision with nothing left, a contradiction rather than a state.
    // ("Expired" cannot go on the fixed track at all: rule 51 leaves about 47px, and a real
    // browser renders it "Expir…".) The visible text stays inside the accessible name either
    // way, which is what WCAG 2.5.3 asks for.
    const narrow = draw(SPENT);
    const short = screen.getByRole("button", { name: SPENT_NAME });
    expect(short.textContent).toContain("Spare");
    expect(short.textContent).not.toContain("0d");
    narrow.unmount();
    // The footer has the room to say it is a repeat.
    draw({ ...SPENT, roomy: true });
    expect(screen.getByRole("button", { name: SPENT_NAME }).textContent).toContain("Spare again");
  });

  it("never claims in a tooltip that a scan will hand the file back", () => {
    // This control knows only THIS item's own spare, so it must not assert what still keeps the
    // file: a season inside a longer show spare is kept regardless, and "still kept until the
    // next scan judges it again" would be false there. It states the fact and the action; the
    // row's chip and KeptByShowNote answer the fate question from the covering spare.
    draw({ ...SPENT, roomy: true });
    const title = screen.getByRole("button", { name: SPENT_NAME }).getAttribute("title") ?? "";
    // Never a keep-until day already gone, either.
    expect(title).not.toMatch(/^Kept until/);
    expect(title).toMatch(/^Your spare expired on .+\. Click to spare it again$/);
    expect(title).not.toMatch(/next scan/);
  });

  it("keeps a way to clear it, in the menu, where the row can say what it is", async () => {
    const onClear = vi.fn();
    const user = userEvent.setup();
    draw({ ...SPENT, onClear, roomy: true });
    await user.click(screen.getByRole("button", { name: "Choose how long to keep it" }));
    await user.click(screen.getByRole("button", { name: "Clear this spare" }));
    expect(onClear).toHaveBeenCalled();
  });

  it("offers no clear row on an item with no spare of its own to clear", async () => {
    draw({}, 30);
    await userEvent
      .setup()
      .click(screen.getByRole("button", { name: "Choose how long to keep it" }));
    expect(screen.queryByRole("button", { name: "Clear this spare" })).not.toBeInTheDocument();
  });
});

describe("the resting mark", () => {
  it("draws nothing for a spent spare, so it cannot contradict the button", () => {
    // The mark is what a row carrying a decision looks like at rest (rule 46). A spent spare is
    // no longer a decision in force at this level -- the button it hands over to on hover
    // offers a fresh one -- so resting as "0d" announced a decision the control no longer
    // holds. A live spare and a forever one still rest as their own icon.
    const { container: spent } = render(
      <OverrideMark override="spare" spareExpiresAt={inDays(-3)} />,
    );
    expect(spent).toBeEmptyDOMElement();

    const { container: live } = render(
      <OverrideMark override="spare" spareExpiresAt={inDays(27)} />,
    );
    expect(live.textContent).toContain("27d");

    const { container: ever } = render(<OverrideMark override="spare" spareExpiresAt={null} />);
    expect(ever.textContent).toContain("∞");
  });
});

// The spare-length menu is portaled to <body>, so it is the one popover in the app where the DOM
// order and the visual anchor have nothing to do with each other. Opening it and pressing Tab used
// to walk on to the next control INSIDE the card, leaving the menu's own rows reachable only by
// tabbing through the remainder of the page -- on the control that decides how long a file is kept.
describe("the spare-length menu's keyboard reach", () => {
  it("takes focus when it opens, since the portal puts it out of Tab's reach", async () => {
    const user = userEvent.setup();
    draw({}, 30);
    await user.click(screen.getByRole("button", { name: "Choose how long to keep it" }));
    expect(screen.getByRole("group", { name: "Spare this item for" })).toHaveFocus();
  });

  it("hands focus back to the caret on Escape, which starts no mutation", async () => {
    const user = userEvent.setup();
    draw({}, 30);
    const caret = screen.getByRole("button", { name: CARET });
    await user.click(caret);
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("group", { name: "Spare this item for" })).not.toBeInTheDocument();
    expect(caret).toHaveFocus();
  });

  // The exits above and below are NOT the same exit. Escape and Back leave the control untouched,
  // so the hook's own restore lands. A pick and a Clear each start a mutation, and `pending`
  // disables the caret in the same batched commit that closes the menu -- so the restore fires at
  // a disabled button, `.focus()` silently does nothing, and the operator who just decided to KEEP
  // a file is left on <body> with the next Tab restarting above the whole queue. This suite once
  // covered only Escape, and was green while the two commonest exits were broken.
  it("hands focus back to the caret after a pick, once the mutation settles", async () => {
    const user = userEvent.setup();
    render(<DrawLive />);
    const caret = screen.getByRole("button", { name: CARET });
    await user.click(caret);

    // The default row wears a "Default" tag, so its name is not the bare label.
    await user.click(screen.getByRole("button", { name: /^30 days/ }));

    expect(screen.queryByRole("group", { name: "Spare this item for" })).not.toBeInTheDocument();
    await waitFor(() => expect(caret).toBeEnabled());
    expect(caret).toHaveFocus();
  });

  it("hands focus back to the caret after Clear this spare", async () => {
    const user = userEvent.setup();
    render(<DrawLive override="spare" />);
    const caret = screen.getByRole("button", { name: CARET });
    await user.click(caret);

    await user.click(screen.getByRole("button", { name: "Clear this spare" }));

    await waitFor(() => expect(caret).toBeEnabled());
    expect(caret).toHaveFocus();
  });

  it("consumes Escape rather than letting the panel behind it close too", async () => {
    // This menu is opened from inside a `.why` panel's own footer, and WhyShell's Escape sits on
    // `window` -- which `document` bubbles on to. Without the menu consuming the key, one press
    // closed the menu AND took away the reasoning the operator was reading (rule 72: the filter
    // popovers in ReviewQueue stop the same key for the same reason).
    const user = userEvent.setup();
    draw({}, 30);
    await user.click(screen.getByRole("button", { name: CARET }));

    const onWindow = vi.fn();
    window.addEventListener("keydown", onWindow);
    await user.keyboard("{Escape}");
    window.removeEventListener("keydown", onWindow);

    expect(screen.queryByRole("group", { name: "Spare this item for" })).not.toBeInTheDocument();
    expect(onWindow).not.toHaveBeenCalled();
  });

  it("keeps Tab inside itself while it is open", async () => {
    const user = userEvent.setup();
    draw({}, 30);
    await user.click(screen.getByRole("button", { name: "Choose how long to keep it" }));
    const menu = screen.getByRole("group", { name: "Spare this item for" });
    const rows = within(menu).getAllByRole("button");
    rows[rows.length - 1]!.focus();
    await user.tab();
    expect(rows[0]!).toHaveFocus();
  });

  // The roles it used to claim promised arrow-key navigation between items that was never
  // implemented. Announcing a contract the widget does not keep is worse than announcing none.
  it("claims no ARIA menu contract it does not implement", async () => {
    const user = userEvent.setup();
    draw({}, 30);
    const caret = screen.getByRole("button", { name: "Choose how long to keep it" });
    expect(caret).not.toHaveAttribute("aria-haspopup");
    await user.click(caret);
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(screen.queryAllByRole("menuitem")).toHaveLength(0);
    // What replaces it: the caret says what it controls, which the portal otherwise hides.
    expect(caret).toHaveAttribute("aria-expanded", "true");
    expect(caret.getAttribute("aria-controls")).toBe(
      screen.getByRole("group", { name: "Spare this item for" }).id,
    );
  });
});
