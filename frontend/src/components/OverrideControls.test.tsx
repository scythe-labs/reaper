// SPDX-License-Identifier: AGPL-3.0-or-later
// The Spare button's own copy. One rule carries all of it: undecided, the button says what a
// press WILL do (the operator's default length); spared, it says what is in force on THIS item.
// A timed spare must always show its own end date, never the default's glyph: a 90-day spare
// under a Forever default must never read "∞ Spared."
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useEffect, useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { expectNoA11yViolations } from "../test/a11y";
import { renderWithProviders } from "../test/renderWithProviders";
import { OverrideControls, OverrideMark } from "./OverrideControls";
import { QueueSettingsContext, type QueueSettings } from "./queueSettings";

// Spreads the real module for ApiError: describeError's `instanceof ApiError` check throws
// against a mock that answers for `api` alone.
vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  api: { general: vi.fn(), profile: vi.fn() },
}));

/** An ISO instant `days` from now. Negative is a spare whose count has already run down. */
function inDays(days: number): string {
  return new Date(Date.now() + days * 86_400_000).toISOString();
}

function draw(props: Partial<Parameters<typeof OverrideControls>[0]> = {}, defaultSpareDays = 0) {
  const shared: QueueSettings = {
    defaultSpareDays,
    unmeasured: { holdsBack: true, isPending: false, isError: false },
  };
  return renderWithProviders(
    <QueueSettingsContext.Provider value={shared}>
      <OverrideControls
        override={null}
        onSet={vi.fn()}
        onClear={vi.fn()}
        pending={false}
        {...props}
      />
    </QueueSettingsContext.Provider>,
  );
}

/** The ∞ the forever spare wears. Both glyphs are aria-hidden, so they never reach a name. */
const forever = (c: HTMLElement) => c.querySelector(".infinity") !== null;

/** `draw` above passes a constant `pending`, which is the one thing no real caller does: every
 *  one of them threads it from the override mutation (`ReviewQueue`, `WhyPanel`, `ShowPanel`), so
 *  a press that sets a spare disables the control it came from IN THE SAME COMMIT, then settles a
 *  moment later. Focus return must survive that disable-then-settle sequence, which a fixed
 *  `pending={false}` cannot exercise: every menu exit would look like it restores focus. */
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
    <QueueSettingsContext.Provider value={shared}>
      <OverrideControls
        override={override}
        spareExpiresAt={null}
        onSet={() => setPending(true)}
        onClear={() => setPending(true)}
        pending={pending}
      />
    </QueueSettingsContext.Provider>
  );
}
type DrawLiveProps = { defaultSpareDays?: number; override?: "spare" | "reap" | null };

const CARET = "Choose how long to keep it";

describe("the control an operator decides with, audited", () => {
  // Every resting state, because each draws different markup: an undecided pair, a lit Spare, and
  // a Reap the app cannot honor yet, which draws as a dashed-red held reap. A bare `draw()` gives
  // the undecided state for free, so the other two states are driven explicitly here too.
  it.each([
    ["undecided", null],
    ["spared", "spare"],
    ["reaped", "reap"],
  ] as const)("has no accessibility violations when %s", async (_state, override) => {
    const { container } = draw({ override }, 30);
    await expectNoA11yViolations(container);
  });

  it("has none with the spare-length menu open, which is not in the render container", async () => {
    // Audited against `document.body`, not the render container. The menu is the app's only
    // `createPortal`, so it lands OUTSIDE the tree `render()` returns, and an audit scoped to
    // `container` would walk right past it and report clean on a menu it never saw. It carries
    // its own `role="group"` and its own Tab trap, which is exactly the markup worth auditing.
    const user = userEvent.setup();
    draw({}, 30);
    await user.click(screen.getByRole("button", { name: CARET }));
    await screen.findByRole("group", { name: "Spare this item for" });
    await expectNoA11yViolations(document.body);
  });
});

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
    // The visible label is just "87d," since the fixed-width track leaves no room for the
    // word, so the accessible name carries the rest, with the visible text still inside it
    // (WCAG 2.5.3).
    const { container } = draw({ override: "spare", spareExpiresAt: inDays(87) });
    const btn = screen.getByRole("button", { name: "Spared 87d left" });
    expect(btn.textContent).toContain("87d");
    expect(btn.textContent).not.toContain("Spared");
    expect(forever(container)).toBe(false);
  });

  it("describes the ITEM's spare, not the default a press would have applied", () => {
    // The glyph must follow the spare actually in force on this item, not the operator's
    // default: a timed spare wears the clock whatever the default is, even a Forever default.
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
  // operator wants when they open this row is to keep the item again. So it offers a fresh
  // spare, and clearing the spent row moves into the length menu, which has room to say what
  // that does.
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
    // A press on a spent spare must set a fresh one, never clear it: clearing would do the
    // opposite of what the operator opened this row to do.
    const onSet = vi.fn();
    const onClear = vi.fn();
    draw({ ...SPENT, onSet, onClear }, 30);
    await userEvent.setup().click(screen.getByRole("button", { name: SPENT_NAME }));
    expect(onSet).toHaveBeenCalledWith("spare", 30);
    expect(onClear).not.toHaveBeenCalled();
  });

  it("shows the plain word, and names what a press does rather than a status", () => {
    // "0d" is not a smaller amount than "27d," it is none of it, and in a pressed green button
    // it would read as an active decision with nothing left, a contradiction rather than a
    // real state. ("Expired" cannot fit the fixed-width track either: it is about 47px wide,
    // and a real browser renders it "Expir….") The visible text stays inside the accessible
    // name either way, which is what WCAG 2.5.3 asks for.
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
    // This control knows only THIS item's own spare, so it must not assert what still keeps
    // the file: a season inside a longer show spare stays kept regardless, and "still kept
    // until the next scan judges it again" would be false there. It states only the fact and
    // the action; the row's chip and `KeptByShowNote` answer the fate question from the
    // covering spare.
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
    // The mark is what a row carrying a decision looks like at rest. A spent spare is no
    // longer a decision in force at this level: the button it hands over to on hover offers a
    // fresh one, so resting as "0d" would announce a decision the control no longer holds. A
    // live spare and a forever one still rest as their own icon.
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
// order and the visual anchor have nothing to do with each other. Tab must stay inside the menu
// while it is open, or it walks on to the next control INSIDE the card, leaving the menu's own
// rows reachable only by tabbing through the rest of the page, on the control that decides how
// long a file is kept.
describe("the spare-length menu's keyboard reach", () => {
  it("takes focus on its first row when it opens, since the portal puts it out of Tab's reach", async () => {
    // Focus must land on the row itself, NOT the group that wraps it. On iOS a `role="group"`
    // with children is never a `UIAccessibilityElement` (WebKit's
    // `determineIsAccessibilityElement`), so focusing the container would leave the VoiceOver
    // cursor with nowhere to be, and a swipe-right from the caret would walk the panel BEHIND
    // this menu instead, since the portal renders this menu's rows last in the DOM. Focusing
    // the APG Menu Button pattern's own control fixes this without changing anything for
    // platforms that were already fine.
    //
    // `dayRows` sorts low to high, so 30 leads for a default of 30 and for one of 90 alike; the
    // second case is what proves this tracks the first ROW rather than the default.
    const user = userEvent.setup();
    draw({}, 30);
    await user.click(screen.getByRole("button", { name: "Choose how long to keep it" }));

    expect(screen.getByRole("group", { name: "Spare this item for" })).not.toHaveFocus();
    expect(screen.getByRole("button", { name: /^30 days/ })).toHaveFocus();
  });

  it("leads with the lowest row even when the operator's default is not it", async () => {
    // The first row is a position in the list, and a fixture whose default happens to BE the
    // first row cannot tell that apart from a hook that focuses the default instead.
    const user = userEvent.setup();
    draw({}, 90);
    await user.click(screen.getByRole("button", { name: "Choose how long to keep it" }));

    expect(screen.getByRole("button", { name: /^30 days/ })).toHaveFocus();
  });

  // The custom-length box is the spare menu's own field. It uses the shared `FixedQuantity`
  // control, and these two tests check what that control must carry across from any hand-built
  // version: it opens focused, and Enter commits.
  describe("the custom spare length", () => {
    /** Open the menu and reveal the custom box. */
    async function openCustom(user: ReturnType<typeof userEvent.setup>) {
      await user.click(screen.getByRole("button", { name: CARET }));
      await user.click(screen.getByRole("button", { name: /Custom length/ }));
      return screen.getByRole("spinbutton", { name: "Custom spare length" });
    }

    it("opens focused, so a length can be typed without reaching for the box", async () => {
      const user = userEvent.setup();
      draw({}, 30);
      expect(await openCustom(user)).toHaveFocus();
    });

    it("commits on Enter, in days", async () => {
      const user = userEvent.setup();
      const onSet = vi.fn();
      draw({ onSet }, 30);
      const box = await openCustom(user);
      await user.clear(box);
      await user.type(box, "45");
      await user.keyboard("{Enter}");
      expect(onSet).toHaveBeenCalledWith("spare", 45);
    });

    // The unit is bound as the box's DESCRIPTION rather than folded into its name, which is why
    // `FixedQuantity` exists in the shape it does: a label carrying "in days" instead would read
    // twice once combined with the real control's own suffix.
    it("speaks its unit once, after the value", async () => {
      const user = userEvent.setup();
      draw({}, 30);
      const box = await openCustom(user);
      expect(box).toHaveAccessibleName("Custom spare length");
      expect(box).toHaveAccessibleDescription("days");
    });
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
  // so the hook's own restore lands normally. A pick and a Clear each start a mutation, and
  // `pending` disables the caret in the same batched commit that closes the menu, so the restore
  // must wait for that mutation to settle: firing it at a disabled button makes `.focus()`
  // silently do nothing, leaving the operator who just decided to KEEP a file on <body> with the
  // next Tab restarting above the whole queue.
  it("hands focus back to the caret after a pick, once the mutation settles", async () => {
    const user = userEvent.setup();
    renderWithProviders(<DrawLive />);
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
    renderWithProviders(<DrawLive override="spare" />);
    const caret = screen.getByRole("button", { name: CARET });
    await user.click(caret);

    await user.click(screen.getByRole("button", { name: "Clear this spare" }));

    await waitFor(() => expect(caret).toBeEnabled());
    expect(caret).toHaveFocus();
  });

  it("consumes Escape rather than letting the panel behind it close too", async () => {
    // This menu is opened from inside a `.why` panel's own footer, and `WhyShell`'s Escape
    // listener sits on `window`, which `document` bubbles on to. If the menu did not consume
    // the key, one press would close the menu AND take away the reasoning the operator was
    // reading. The filter popovers in `ReviewQueue` stop the same key for the same reason.
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

  // ARIA menu/menuitem roles promise arrow-key navigation between items, which this widget does
  // not implement. Announcing a contract the widget does not keep is worse than announcing none.
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
