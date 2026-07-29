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
import { FixedQuantity, QuantityInput, SIZE_UNITS, TIME_UNITS } from "./QuantityInput";

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

const box = () => screen.getByLabelText("Most titles per run");

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
      <QuantityInput value={60} units={TIME_UNITS} onChange={vi.fn()} ariaLabel="Grace period" />,
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
      <QuantityInput value={60} units={TIME_UNITS} onChange={vi.fn()} ariaLabel="Grace period" />,
    );
    expect(screen.getByLabelText("Grace period")).toHaveValue(2);
    expect(screen.getByLabelText("Grace period unit")).toHaveValue("months");

    rerender(
      <QuantityInput value={7} units={TIME_UNITS} onChange={vi.fn()} ariaLabel="Grace period" />,
    );

    expect(screen.getByLabelText("Grace period unit")).toHaveValue("weeks");
    expect(screen.getByLabelText("Grace period")).toHaveValue(1);
  });

  it("holds still while that same box is being typed in", async () => {
    // The box's own emits are remembered, so typing a fraction of the current unit never
    // jumps the dropdown out from under the caret.
    const user = userEvent.setup();
    function Sized() {
      const [value, setValue] = useState(1e9);
      return (
        <QuantityInput
          value={value}
          units={SIZE_UNITS}
          onChange={setValue}
          ariaLabel="Most disk freed per run"
        />
      );
    }
    render(<Sized />);
    const size = screen.getByLabelText("Most disk freed per run");

    await user.clear(size);
    await user.type(size, "0.5");
    await user.tab();

    expect(screen.getByLabelText("Most disk freed per run unit")).toHaveValue("GB");
    expect(size).toHaveValue(0.5);
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
    function Sized() {
      const [value, setValue] = useState(1e9);
      return (
        <QuantityInput
          value={value}
          units={SIZE_UNITS}
          onChange={(n) => {
            setValue(n);
            emit(n);
          }}
          ariaLabel="Most disk freed per run"
        />
      );
    }
    render(<Sized />);
    const size = screen.getByLabelText("Most disk freed per run");

    await user.clear(size);
    await user.type(size, "0");
    await user.tab();

    expect(size).toHaveValue(0.01);
    expect(screen.getByLabelText("Most disk freed per run unit")).toHaveValue("MB");
    expect(emit).toHaveBeenLastCalledWith(1e4);
  });
});
