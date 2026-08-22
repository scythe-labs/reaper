// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The number-with-a-unit control: what the box does while a number is being typed. Clearing
// it to retype must not fold the old digits into the new ones, a half-typed decimal must
// survive long enough to finish, and the unit a value is shown in must follow a value that
// was replaced from outside.
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

/** The rating bar's exact shape: tenths stored, one decimal shown, converted on the way in. */
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

/** The changeable-unit twin of `Fixed`: a size box wired the way PolicyEditor wires the two
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
    // B7: the box used to read an empty field as 0, write "1" back under the caret, and let
    // the digits typed next land after it -- select-all + "25" stored 125.
    const user = userEvent.setup();
    const emit = vi.fn();
    render(<Fixed initial={100} min={1} max={1000} onEmit={emit} />);

    await user.clear(box());
    await user.type(box(), "25");
    await user.tab();

    expect(emit).toHaveBeenLastCalledWith(25);
    expect(box()).toHaveValue(25);
    // The zero is the whole defect: it is what the call site re-floored to 1, which React
    // then wrote back into the box for "25" to land after.
    expect(emit).not.toHaveBeenCalledWith(0);
  });

  it("says nothing at all while the box is empty", async () => {
    // An empty box is someone midway through retyping, never a zero -- and never a floor
    // the call site invents to cover for the zero.
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
    // PR5: the ceiling the server enforces, applied in the box, so an out-of-range number
    // never comes home as a validator's sentence.
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
    // #296: seven policy boxes declare no `step`, which in HTML already means 1, and every one
    // is backed by an `int`. The browser does not enforce it -- Chrome marks a typed 1.5
    // `stepMismatch` and hands the change handler "1.5" anyway, because step is checked at form
    // validation and this control never submits a form -- so 1.5 reached the draft and came
    // home as "Input should be a valid integer, got a number with a fractional part", a
    // validator's sentence on a saveable-looking form (rule 21).
    const user = userEvent.setup();
    const emit = vi.fn();
    render(<Fixed initial={40} min={1} max={1000} onEmit={emit} />);

    await user.clear(box());
    await user.type(box(), "1.5");
    await user.tab();

    expect(emit).not.toHaveBeenCalledWith(1.5);
    // The "1" typed on the way there is a real value and is kept: nothing is rounded, because
    // rounding needs a direction this control cannot have -- half its call sites are caps and
    // half are protections, and the safe direction is opposite for the two.
    expect(emit).toHaveBeenLastCalledWith(1);
    expect(box()).toHaveValue(1);
  });

  it("leaves the stored number alone when the fraction is all that was typed", async () => {
    // ".5" never passes through a whole number on its way in, so there is nothing to keep and
    // the box goes back to what was stored -- the same thing it does with an abandoned "7.".
    // The floor is 0 here so the blur clamp cannot fire: this is about what the box withholds,
    // and a box whose floor DOES bite pulls the value up to it, which the floor test covers.
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
    // The vote floor ships `step={100}` so its spinner moves in hundreds. 250 is a legal floor,
    // and snapping it to the grid would rewrite the operator's own number on a field that
    // decides what survives -- the deletion-path version of the bug above. Only decimals are
    // taken from a step, which for 100 is none.
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
    // B8: the box re-derived its text from stored tenths on every render, so "7" became
    // "7.0" under the caret and the "." that followed made "7.0." -- which a number input
    // reports as empty, which the old coercion stored as a floor of 0.0.
    const user = userEvent.setup();
    const emit = vi.fn();
    render(<RatingBar onEmit={emit} />);
    const bar = screen.getByLabelText("Rating score out of 10");

    await user.clear(bar);
    await user.type(bar, "7.5");
    await user.tab();

    expect(emit).toHaveBeenLastCalledWith(75);
    expect(bar).toHaveValue(7.5);
    // Never, at any keystroke, a floor of zero: the "." used to report the box as empty,
    // which the old coercion stored as 0.0 -- a bar that keeps nothing.
    expect(emit).not.toHaveBeenCalledWith(0);
  });

  it("takes tenths but not hundredths, because the field behind it holds tenths", async () => {
    // The tenths box is the case that proves the bound is read off `step` rather than hardcoded
    // to whole numbers: 7.5 is a real score here and must still go through, while the digit
    // past it is withheld the same way a fraction is in a whole-number box.
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
    // #176: the suffix was `aria-hidden`, so the box announced "Most titles per run, 40" and
    // the unit lived on screen and nowhere else. It arrives as the DESCRIPTION, read after
    // the value ("40, titles"), so the eleven call sites whose name already says the unit do
    // not stutter it (rule 21 binds a spoken string as hard as a printed one).
    render(<Fixed initial={40} />);

    expect(box()).toHaveAccessibleName("Most titles per run");
    expect(box()).toHaveAccessibleDescription("titles");
  });

  it("describes the box with the suffix that is on screen, not a second copy of it", () => {
    // The description points AT the rendered suffix, so a unit cannot be right in the box and
    // wrong in the announcement -- there is one copy of the word, not two that drift (rule
    // 144). "/ 10" is the case a table of spoken units would have had to carry separately.
    render(<RatingBar onEmit={vi.fn()} />);
    const bar = screen.getByLabelText("Rating score out of 10");

    expect(bar).toHaveAccessibleDescription("/ 10");
    expect(screen.getByText("/ 10")).toHaveAttribute("id", bar.getAttribute("aria-describedby"));
  });

  it("leaves the changeable-unit box undescribed, because its unit is a control", () => {
    // Rule 72's twin, deliberately not swept and pinned so the deferral is not silently
    // undone: here the unit is a real <select> beside the number that names itself and
    // announces the unit as its own value. Describing the number with it as well would say
    // the unit twice on the way through the pair.
    render(
      <QuantityInput value={60} units={timeUnits()} onChange={vi.fn()} ariaLabel="Grace period" />,
    );

    expect(screen.getByLabelText("Grace period")).not.toHaveAttribute("aria-describedby");
    expect(screen.getByLabelText("Grace period unit")).toHaveValue("months");
  });
});

describe("the unit a value is shown in", () => {
  it("follows a value replaced from outside", () => {
    // U17: the unit was picked once, on mount, so a preset staging 7 days into a box left
    // on months read "0.23 months" -- right, and unreadable.
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
    // The floor is in base units (1 byte) and the box draws two decimals of GB, so the clamp
    // stored 1 byte and drew it as "0": a box reading zero beside a sentence saying "at most
    // 1 B per run". Lifting the floor to the SHOWN unit fixed the display by raising the
    // stored cap -- 0.01 GB, then 0.01 TB in a TB box, ten gigabytes of permitted deletion
    // where the operator asked for none. So the floor drops to what the smallest unit can
    // draw and the box switches to that unit (rule 31: the bound with less deletion
    // pressure). 0.01 MB is 10 KB, which no media file fits inside.
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
    // #251: the box draws two decimals of GB and the emit kept every digit typed, so 1.234
    // stored 1_234_000_000 and then redrew "1.23". The cap in force sat 4 MB above the one on
    // screen -- on the control whose whole job is to state a bound. No clamp and no unit
    // switch are involved: three decimals in the shown unit is the whole reproduction.
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
    // Rule 31: a precision reduction on a field that can add deletion pressure takes the bound
    // with LESS pressure. `trim` rounds to nearest, so reusing it on the way in would round
    // this cap up to 1.24 GB -- 4 MB of deletion the operator never authorized. Cutting is the
    // half of the fix that has a direction, and this is the case that tells them apart.
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
    // The cut reads the decimal text because the arithmetic form does not survive binary
    // floating point: `Math.floor(0.29 * 100) / 100` is 0.28, since 0.29 * 100 is
    // 28.999999999999996. Storing 0.28 over a typed 0.29 would be this same defect pointing
    // the other way, and it is a whole display step wide rather than a hidden digit.
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
    // `bestUnit` picks the largest unit the value clears, so a value of exactly one whole
    // unit is what every round policy default looks like: 365, 30, 7. The box read
    // "1 years" on the page where deletion rules are written (#415).
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
    // "1 GBs" would be the same defect introduced by the fix for it.
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
