// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The settings row: a name, a line of help under it, and the control to the right. It
// replaces several settings panels' own hand-written chrome around each control, each copy
// four lines long, with one shared grammar.
//
// The chrome is not decoration. `.set-label`, `.help` and `.set-control` are the three grid
// areas `styles/27-settings-rows.css` places, and the control track is a fixed width so
// every box on the page is the same size (the measured reason is in that stylesheet). A
// hand-written row that nests them wrong, or drops `.help` out of column one, loses that
// silently. It still reads as a settings row, at a different width from its neighbors.
//
// `help` is required because the app's grammar is exactly one help paragraph per control,
// always present and bound directly beneath it. A row with nothing to say has a `label`
// that already says it.
//
// This deliberately does not render the control's accessible name from `label`. `.set-label`
// is a `<span>` and names nothing, so every control inside still carries its own
// `aria-label`, and two component tests reach controls that way. Naming them from here would
// need a `<label htmlFor>` and an id per row, which is a different grammar from the one the
// stylesheet places.

import type { ReactNode } from "react";

export function SetRow({
  label,
  help,
  /** The control itself. It carries its own `aria-label`: the label beside it is a span. */
  children,
  /** The two named opt-outs the stylesheet defines, plus the accent row's own block layout.
   *  Not a free-form class name: a fourth variant is a stylesheet change, not a caller's
   *  choice. */
  variant,
  /** Faded, for a row parked behind a switch that is off. `28-settings-log.css` dims the label
   *  and the help and leaves the control alone. */
  dim = false,
  /** A second class on the control box, where one control needs its own layout inside the
   *  fixed track. One caller uses it, and it is the one prop here that a caller can spell
   *  freely, so reach for a `variant` first and add this only where the class styles what
   *  is inside the track rather than the track itself. Its one caller is the unlinked
   *  `ServerPickList` row, a branch `PlexPanel.test.tsx` names as one its fixture does not
   *  mount, so nothing pins this arm today. `variant` and `dim` are pinned in
   *  `GeneralPanel.test.tsx`. */
  controlClass,
  /** Anything belonging inside the row but below the control: a refusal message, a preset
   *  strip, a warning about the control just set. It renders inside the row element on
   *  purpose, since a notice about a control has to stay in the same box as the control, and
   *  `PlexPanel.test.tsx` asserts exactly that with `closest(".set-row")`. */
  after,
}: {
  label: ReactNode;
  help: ReactNode;
  children: ReactNode;
  variant?: "plain" | "cluster" | "accent";
  dim?: boolean;
  controlClass?: string;
  after?: ReactNode;
}) {
  const classes = ["set-row"];
  if (variant === "plain") classes.push("set-row-plain");
  if (variant === "cluster") classes.push("set-row-cluster");
  if (variant === "accent") classes.push("accent-row");
  if (dim) classes.push("dim");
  return (
    <div className={classes.join(" ")}>
      <span className="set-label">{label}</span>
      <p className="help">{help}</p>
      <div className={controlClass ? `set-control ${controlClass}` : "set-control"}>{children}</div>
      {after}
    </div>
  );
}
