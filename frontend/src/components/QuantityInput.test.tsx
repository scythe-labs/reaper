// SPDX-License-Identifier: AGPL-3.0-or-later
//
// This file covers the number-with-a-unit control, and what the box does while a number is
// being typed. Clearing it to retype must not fold the old digits into the new ones, a
// half-typed decimal must survive long enough to finish, and the unit a value is shown in must
// follow a value that was replaced from outside.
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { FixedQuantity, QuantityInput, sizeUnits, timeUnits } from "./QuantityInput";

/** A parent that stores what the box emits, the way every real call site does. */
function Fixed({
  initial,
  onEmit,
  ...rest
}: {
  initial: number;
  onEmit?: (n: number) => void;
  min?: number;
  max?: number;
  step?: number;
}) {
  const [value, setValue] = useState(initial);
  return (
    <FixedQuantity
      value={value}
      suffix="titles"
      ariaLabel="Most titles per run"
      onChange={(n) => {
        setValue(n);
        onEmit?.(n);
      }}
      {...rest}
    />
  );
}

/** The rating bar's exact shape. Tenths are stored, one decimal is shown, and the value is
 *  converted on the way in. */
function RatingBar({ onEmit }: { onEmit: (tenths: number) => void }) {
  const [floor, setFloor] = useState(65);
  return (
    <FixedQuantity
      value={(floor / 10).toFixed(1)}
      suffix="/ 10"
      min={0}
      max={10}
      step={0.1}
      ariaLabel="Rating score out of 10"
      onChange={(v) => {
        const tenths = Math.round(v * 10);
        setFloor(tenths);
        onEmit(tenths);
      }}
    />
  );
}

/** The changeable-unit twin of `Fixed`. A size box wired the way PolicyEditor wires the two
 *  deletion caps, storing whatever the box emits. */
function Sized({ initial = 1e9, onEmit }: { initial?: number; onEmit?: (n: number) => void }) {
  const [value, setValue] = useState(initial);
  return (
    <QuantityInput
      value={value}
      units={sizeUnits()}
      ariaLabel="Most disk freed per run"
      onChange={(n) => {
        setValue(n);
        onEmit?.(n);
      }}
    />
  );
}

const box = () => screen.getByLabelText("Most titles per run");
const size = () => screen.getByLabelText("Most disk freed per run");
const sizeUnit = () => screen.getByLabelText("Most disk freed per run unit");

describe("typing a number into a box that already has one", () => {
  it("replaces what was there instead of appending to it", async () => {
    // An empty field must never be read as 0. Reading it as 0 and clamping to the floor would
    // write a digit back into the box under the caret, so the digits typed next would land
    // after it, turning "25" into 125.
    const user = userEvent.setup();
    const emit = vi.fn();
    render(<Fixed initial={100} min={1} max={1000} onEmit={emit} />);

    await user.clear(box());
    await user.type(box(), "25");
    await user.tab();

    expect(emit).toHaveBeenLastCalledWith(25);
    expect(box()).toHaveValue(25);
    // The zero is the whole risk here. It is what a call site would re-floor to 1, which React
    // would then write back into the box for "25" to land after.
    expect(emit).not.toHaveBeenCalledWith(0);
  });

  it("says nothing at all while the box is empty", async () => {
    // An empty box means someone is midway through retyping. It is never a zero, and never a
    // floor value invented to cover for a zero.
    const user = userEvent.setup();
    const emit = vi.fn();
    render(<Fixed initial={100} min={1} onEmit={emit} />);

    await user.clear(box());

    expect(emit).not.toHaveBeenCalled();
    expect(box()).toHaveValue(null);
  });

  it("puts the stored number back when the box is left empty", async () => {
    const user = userEvent.setup();
    const emit = vi.fn();
    render(<Fixed initial={100} min={1} onEmit={emit} />);

    await user.clear(box());
    await user.tab();

    expect(emit).not.toHaveBeenCalled();
    expect(box()).toHaveValue(100);
  });

  it("pulls a number under the floor up when the box is left", async () => {
    const user = userEvent.setup();
    const emit = vi.fn();
    render(<Fixed initial={100} min={1} onEmit={emit} />);

    await user.clear(box());
    await user.type(box(), "0");
    await user.tab();

    expect(emit).toHaveBeenLastCalledWith(1);
  });

  it("pulls a number over the ceiling down when the box is left", async () => {
    // The box applies the same ceiling the server enforces, so an out-of-range number never
    // comes back as a validator's error sentence.
    const user = userEvent.setup();
    const emit = vi.fn();
    render(<Fixed initial={100} min={1} max={1000} onEmit={emit} />);

    await user.clear(box());
    await user.type(box(), "9999");
    await user.tab();

    expect(emit).toHaveBeenLastCalledWith(1000);
  });
});

describe("a number the field behind the box cannot hold", () => {
  it("never lets a fraction out of a whole-number box", async () => {
    // Seven policy boxes declare no `step`, which in HTML already means 1, and every one is
    // backed by an `int`. The browser does not enforce that on its own. Chrome marks a typed
    // 1.5 as `stepMismatch` and hands the change handler "1.5" anyway, because step is only
    // checked at form validation, and this control never submits a form. Without a guard, 1.5
    // would reach the draft and come back as "Input should be a valid integer, got a number
    // with a fractional part," a validator's sentence on a form that otherwise looks ready to
    // save.
    const user = userEvent.setup();
    const emit = vi.fn();
    render(<Fixed initial={40} min={1} max={1000} onEmit={emit} />);

    await user.clear(box());
    await user.type(box(), "1.5");
    await user.tab();

    expect(emit).not.toHaveBeenCalledWith(1.5);
    // The "1" typed on the way there is a real value and is kept. Nothing here is rounded,
    // because rounding needs a direction this control cannot assume. Half its call sites are
    // caps and half are protections, and the safe rounding direction is opposite for the two.
    expect(emit).toHaveBeenLastCalledWith(1);
    expect(box()).toHaveValue(1);
  });

  it("leaves the stored number alone when the fraction is all that was typed", async () => {
    // ".5" never passes through a whole number on its way in, so there is nothing to keep, and
    // the box goes back to what was stored. It does the same thing with an abandoned "7.". The
    // floor here is 0, so the blur clamp cannot fire. This test is about what the box
    // withholds. A box whose floor does bite pulls the value up to it instead, which the floor
    // test above covers.
    const user = userEvent.setup();
    const emit = vi.fn();
    render(<Fixed initial={40} min={0} max={1000} onEmit={emit} />);

    await user.clear(box());
    await user.type(box(), ".5");
    await user.tab();

    expect(emit).not.toHaveBeenCalled();
    expect(box()).toHaveValue(40);
  });

  it("reads step as a precision, not as a ladder the value has to land on", async () => {
    // The vote floor ships `step={100}`, so its spinner moves in hundreds. 250 is a legal floor
    // value, and snapping it to the nearest step would silently rewrite the operator's own
    // number on a field that decides what survives, the same risk as the test above but on a
    // deletion-relevant field. Only decimals are taken from a step value, and for a step of 100
    // that is none.
    const user = userEvent.setup();
    const emit = vi.fn();
    render(<Fixed initial={100} min={0} step={100} onEmit={emit} />);

    await user.clear(box());
    await user.type(box(), "250");
    await user.tab();

    expect(emit).toHaveBeenLastCalledWith(250);
    expect(box()).toHaveValue(250);
  });
});

describe("the rating bar", () => {
  it("lets a half score be typed", async () => {
    // If the box re-derived its text from stored tenths on every render, typing "7" would
    // become "7.0" under the caret, and the "." typed next would make "7.0.", which a number
    // input reports as empty. An empty field must never be stored as a floor of 0.0.
    const user = userEvent.setup();
    const emit = vi.fn();
    render(<RatingBar onEmit={emit} />);
    const bar = screen.getByLabelText("Rating score out of 10");

    await user.clear(bar);
    await user.type(bar, "7.5");
    await user.tab();

    expect(emit).toHaveBeenLastCalledWith(75);
    expect(bar).toHaveValue(7.5);
    // At no keystroke does this store a floor of zero. A lone "." reports the box as empty,
    // and an empty box must never be stored as 0.0, a rating bar that keeps nothing.
    expect(emit).not.toHaveBeenCalledWith(0);
  });

  it("takes tenths but not hundredths, because the field behind it holds tenths", async () => {
    // The tenths box proves the decimal bound is read off `step` rather than hardcoded to whole
    // numbers. 7.5 is a real score here and must still go through, while the digit past it is
    // withheld the same way a fraction is in a whole-number box.
    const user = userEvent.setup();
    const emit = vi.fn();
    render(<RatingBar onEmit={emit} />);
    const bar = screen.getByLabelText("Rating score out of 10");

    await user.clear(bar);
    await user.type(bar, "7.55");
    await user.tab();

    expect(emit).toHaveBeenLastCalledWith(75);
    expect(bar).toHaveValue(7.5);
  });

  it("keeps the stored score when the box is cleared and abandoned", async () => {
    const user = userEvent.setup();
    const emit = vi.fn();
    render(<RatingBar onEmit={emit} />);
    const bar = screen.getByLabelText("Rating score out of 10");

    await user.clear(bar);
    await user.tab();

    expect(emit).not.toHaveBeenCalled();
    expect(bar).toHaveValue(6.5);
  });
});

describe("the unit a screen reader hears", () => {
  it("gives the fixed-suffix box its unit, and does not repeat it in the name", () => {
    // The suffix must not be `aria-hidden`, or the box would announce only "Most titles per
    // run, 40" and the unit would exist on screen and nowhere else. The unit arrives as the
    // accessible description, read after the value ("40, titles"), so the eleven call sites
    // whose name already says the unit do not repeat it.
    render(<Fixed initial={40} />);

    expect(box()).toHaveAccessibleName("Most titles per run");
    expect(box()).toHaveAccessibleDescription("titles");
  });

  it("describes the box with the suffix that is on screen, not a second copy of it", () => {
    // The description points at the rendered suffix, so the unit cannot be right in the box and
    // wrong in the announcement. There is one copy of the word, not two that could drift apart.
    // "/ 10" is the case a separate table of spoken units would have had to carry on its own.
    render(<RatingBar onEmit={vi.fn()} />);
    const bar = screen.getByLabelText("Rating score out of 10");

    expect(bar).toHaveAccessibleDescription("/ 10");
    expect(screen.getByText("/ 10")).toHaveAttribute("id", bar.getAttribute("aria-describedby"));
  });

  it("leaves the changeable-unit box undescribed, because its unit is a control", () => {
    // This is a deliberate exception, pinned so it is not silently swept away later. Here the
    // unit is a real <select> beside the number, and it names itself and announces the unit as
    // its own value. Describing the number with it as well would say the unit twice.
    render(
      <QuantityInput value={60} units={timeUnits()} onChange={vi.fn()} ariaLabel="Grace period" />,
    );

    expect(screen.getByLabelText("Grace period")).not.toHaveAttribute("aria-describedby");
    expect(screen.getByLabelText("Grace period unit")).toHaveValue("months");
  });
});

describe("the unit a value is shown in", () => {
  it("follows a value replaced from outside", () => {
    // The unit must not be picked once on mount. If it were, a preset staging 7 days into a
    // box left on months would read "0.23 months", technically correct but unreadable.
    const { rerender } = render(
      <QuantityInput value={60} units={timeUnits()} onChange={vi.fn()} ariaLabel="Grace period" />,
    );
    expect(screen.getByLabelText("Grace period")).toHaveValue(2);
    expect(screen.getByLabelText("Grace period unit")).toHaveValue("months");

    rerender(
      <QuantityInput value={7} units={timeUnits()} onChange={vi.fn()} ariaLabel="Grace period" />,
    );

    expect(screen.getByLabelText("Grace period unit")).toHaveValue("weeks");
    expect(screen.getByLabelText("Grace period")).toHaveValue(1);
  });

  it("holds still while that same box is being typed in", async () => {
    // The box's own emits are remembered, so typing a fraction of the current unit never
    // jumps the dropdown out from under the caret.
    const user = userEvent.setup();
    render(<Sized />);

    await user.clear(size());
    await user.type(size(), "0.5");
    await user.tab();

    expect(sizeUnit()).toHaveValue("GB");
    expect(size()).toHaveValue(0.5);
  });

  it("clamps a typed 0 down to the smallest unit, not up to the shown one", async () => {
    // The floor is stored in base units (1 byte), and the box draws two decimals of GB, so a
    // naive clamp would store 1 byte and draw it as "0", a box reading zero beside a sentence
    // saying "at most 1 B per run". Raising the floor to match the shown unit instead would
    // raise the actual stored cap along with it, 0.01 GB, then 0.01 TB in a TB box, permitting
    // ten gigabytes of deletion where the operator asked for none. So instead the floor drops
    // to whatever the smallest unit can draw, and the box switches to that unit, since a lower
    // floor deletes less. 0.01 MB is 10 KB, which no media file fits inside.
    const user = userEvent.setup();
    const emit = vi.fn();
    render(<Sized onEmit={emit} />);

    await user.clear(size());
    await user.type(size(), "0");
    await user.tab();

    expect(size()).toHaveValue(0.01);
    expect(sizeUnit()).toHaveValue("MB");
    expect(emit).toHaveBeenLastCalledWith(1e4);
  });
});

describe("the number the box stores, against the number it shows", () => {
  it("drops the digits it will not draw instead of storing them", async () => {
    // The box draws two decimals of GB. If the emit kept every digit typed, 1.234 would store
    // 1_234_000_000 and then redraw as "1.23", so the cap actually in force would sit 4 MB
    // above the one shown on screen, on a control whose whole job is to state a bound. No clamp
    // and no unit switch need be involved. Three decimals in the shown unit is enough to
    // reproduce this on its own.
    const user = userEvent.setup();
    const emit = vi.fn();
    render(<Sized onEmit={emit} />);

    await user.clear(size());
    await user.type(size(), "1.234");
    await user.tab();

    expect(emit).toHaveBeenLastCalledWith(1.23e9);
    expect(size()).toHaveValue(1.23);
    expect(sizeUnit()).toHaveValue("GB");
  });

  it("cuts the digits off rather than rounding them up", async () => {
    // A precision reduction on a field that can permit more deletion must always round toward
    // less deletion, never more. `trim` rounds to nearest, so using it here would round this
    // cap up to 1.24 GB, 4 MB of deletion the operator never authorized. Cutting the digits
    // instead of rounding is what gives this a safe direction, and this test is the case that
    // tells the two approaches apart.
    const user = userEvent.setup();
    const emit = vi.fn();
    render(<Sized onEmit={emit} />);

    await user.clear(size());
    await user.type(size(), "1.236");
    await user.tab();

    expect(emit).toHaveBeenLastCalledWith(1.23e9);
    expect(size()).toHaveValue(1.23);
  });

  it("keeps a two-decimal number the operator really did type", async () => {
    // The cut reads the decimal text instead of doing the arithmetic, because the arithmetic
    // form does not survive binary floating point. `Math.floor(0.29 * 100) / 100` is 0.28,
    // since 0.29 * 100 is 28.999999999999996. Storing 0.28 for a typed 0.29 would be this same
    // defect in the other direction, and here it is a whole display step wide rather than a
    // hidden digit.
    const user = userEvent.setup();
    const emit = vi.fn();
    render(<Sized onEmit={emit} />);

    await user.clear(size());
    await user.type(size(), "0.29");
    await user.tab();

    expect(emit).toHaveBeenLastCalledWith(2.9e8);
    expect(size()).toHaveValue(0.29);
  });
});

describe("how the unit is worded beside the number", () => {
  const unitBox = () => screen.getByLabelText("Minimum dormancy unit") as HTMLSelectElement;

  it("says 'year', not 'years', beside a 1", () => {
    // `bestUnit` picks the largest unit the value clears, so a value of exactly one whole unit
    // is what every round policy default looks like, such as 365, 30, or 7. Without this fix
    // the box would read "1 years" on the page where deletion rules are written.
    render(
      <QuantityInput
        value={365}
        units={timeUnits()}
        onChange={vi.fn()}
        ariaLabel="Minimum dormancy"
      />,
    );

    expect(unitBox()).toHaveValue("years"); // the stored value never inflects
    expect(unitBox().selectedOptions[0]!.textContent).toBe("year");
  });

  it("goes back to the plural for anything that is not one", () => {
    const { rerender } = render(
      <QuantityInput
        value={730}
        units={timeUnits()}
        onChange={vi.fn()}
        ariaLabel="Minimum dormancy"
      />,
    );
    expect(unitBox().selectedOptions[0]!.textContent).toBe("years");

    // Including a fraction, which is the case a bare `=== 1` on the rounded number would miss.
    rerender(
      <QuantityInput
        value={400}
        units={timeUnits()}
        onChange={vi.fn()}
        ariaLabel="Minimum dormancy"
      />,
    );
    expect(screen.getByLabelText("Minimum dormancy")).toHaveValue(1.1);
    expect(unitBox().selectedOptions[0]!.textContent).toBe("years");
  });

  it("leaves the size units alone, because they do not inflect", () => {
    // Pluralizing size units the same way would reintroduce the defect the fix above avoids.
    render(
      <QuantityInput
        value={1e9}
        units={sizeUnits()}
        onChange={vi.fn()}
        ariaLabel="Minimum dormancy"
      />,
    );

    expect(screen.getByLabelText("Minimum dormancy")).toHaveValue(1);
    expect(unitBox().selectedOptions[0]!.textContent).toBe("GB");
  });
});
