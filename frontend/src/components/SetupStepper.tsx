// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The wizard's steps, declared once, and the card every step is drawn in.
//
// The progress row and the "Step 2 of 4" chip are two views of one fact, and the heading of
// each step is a third. Hand-numbering them would be four copies of the same count in four
// files, which is exactly the shape rule 144 warns about -- the copy nobody regenerates is
// the one that goes wrong, and here it would go wrong by telling a new operator the flow is
// shorter or longer than it is. So `SETUP_STEPS` is the declaration and everything else is
// derived from it: adding a step cannot leave a stale total behind.

import type { ReactNode } from "react";

/** The flow, in order. The `key` is what the wizard routes on; the `label` is what the
 *  operator reads in the progress row. */
export const SETUP_STEPS = [
  { key: "password", label: "Password" },
  { key: "plex", label: "Plex" },
  { key: "connect", label: "Connect" },
  { key: "scan", label: "Scan" },
] as const;

export type SetupStepKey = (typeof SETUP_STEPS)[number]["key"];

/** Where a step sits in the flow, 1-based, for the copy that counts. */
export function stepNumber(key: SetupStepKey): number {
  return SETUP_STEPS.findIndex((s) => s.key === key) + 1;
}

/** The progress row.
 *
 *  Each dot says its state in words as well as in color and shape. A tick that differs from
 *  a hollow ring only by hue tells a screen reader nothing, and this is the one screen a new
 *  operator cannot skip past -- the same reasoning the old checklist's tick carried, kept
 *  when the checklist became this. */
function Stepper({ current }: { current: SetupStepKey }) {
  const at = stepNumber(current);
  return (
    // Named, so it is one identifiable thing rather than a bare list of four items -- and so
    // the setup gate's tests have a marker that is on every step and on nothing else.
    <ol className="stepper" aria-label="Setup progress">
      {SETUP_STEPS.map((step, i) => {
        const n = i + 1;
        const state = n < at ? "done" : n === at ? "now" : "";
        return (
          <li key={step.key} className={state}>
            <span
              className="step-dot"
              role="img"
              aria-label={n < at ? "Done" : n === at ? "Current step" : "Not done yet"}
            >
              {n < at ? "✓" : n === at ? n : "○"}
            </span>
            {/* Dropped on a narrow screen for every step but the current one, so four labels
                do not fight for one phone-width line (see 26-setup.css). */}
            <span className="step-label">{step.label}</span>
            {n < SETUP_STEPS.length && <span className="step-rule" aria-hidden="true" />}
          </li>
        );
      })}
    </ol>
  );
}

/** One step's card: the progress row, the heading, its position in the flow, and the step's
 *  own body.
 *
 *  It wears `.modal`'s chrome but is not a modal and must not read as one -- there is no page
 *  behind it, and the first step may not be dismissed at all -- so it has no scrim, no Escape
 *  handler and no closing control. `ModalShell` is deliberately not reused here for that
 *  reason: its whole contract is about dismissal and focus return to an opener that, on the
 *  first screen of a fresh install, does not exist. */
export function StepCard({
  step,
  title,
  children,
}: {
  step: SetupStepKey;
  title: string;
  children: ReactNode;
}) {
  return (
    <section className="step-card">
      <Stepper current={step} />
      <div className="step-head">
        <h2>{title}</h2>
        <span className="step-of">
          Step {stepNumber(step)} of {SETUP_STEPS.length}
        </span>
      </div>
      {children}
    </section>
  );
}
