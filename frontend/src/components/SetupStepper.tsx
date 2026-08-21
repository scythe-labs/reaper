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

import { createContext, useContext, useEffect, useRef, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import i18next from "../i18n";

/** The flow, in order. The `key` is what the wizard routes on; the progress row's word for
 *  each step is `stepLabel` below, read from the catalog rather than carried here (rule 144). */
export const SETUP_STEPS = [
  { key: "password" },
  { key: "plex" },
  { key: "connect" },
  { key: "scan" },
] as const;

export type SetupStepKey = (typeof SETUP_STEPS)[number]["key"];

/** Where a step sits in the flow, 1-based, for the copy that counts. */
export function stepNumber(key: SetupStepKey): number {
  return SETUP_STEPS.findIndex((s) => s.key === key) + 1;
}

/** The progress row's per-step word. One literal `t()` call per step, since a computed key
 *  is unreadable to the missing-key gate -- the same shape `jobMeta` in JobsPanel.tsx uses. */
function stepLabel(key: SetupStepKey): string {
  switch (key) {
    case "password":
      return i18next.t("setup.steps.password");
    case "plex":
      return i18next.t("setup.steps.plex");
    case "connect":
      return i18next.t("setup.steps.connect");
    case "scan":
      return i18next.t("setup.steps.scan");
  }
}

/** The progress row.
 *
 *  Each dot says its state in words as well as in color and shape. A tick that differs from
 *  a hollow ring only by hue tells a screen reader nothing, and this is the one screen a new
 *  operator cannot skip past -- the same reasoning the old checklist's tick carried, kept
 *  when the checklist became this. */
function Stepper({ current }: { current: SetupStepKey }) {
  const { t } = useTranslation();
  const at = stepNumber(current);
  return (
    // Named, so it is one identifiable thing rather than a bare list of four items -- and so
    // the setup gate's tests have a marker that is on every step and on nothing else.
    <ol className="stepper" aria-label={t("setup.stepper.ariaLabel")}>
      {SETUP_STEPS.map((step, i) => {
        const n = i + 1;
        const state = n < at ? "done" : n === at ? "now" : "";
        return (
          <li key={step.key} className={state}>
            <span
              className="step-dot"
              role="img"
              aria-label={
                n < at
                  ? t("setup.stepper.doneAria")
                  : n === at
                    ? t("setup.stepper.currentAria")
                    : t("setup.stepper.notDoneAria")
              }
            >
              {n < at ? "✓" : n === at ? n : "○"}
            </span>
            {/* Dropped on a narrow screen for every step but the current one, so four labels
                do not fight for one phone-width line (see 26-setup.css). */}
            <span className="step-label">{stepLabel(step.key)}</span>
            {n < SETUP_STEPS.length && <span className="step-rule" aria-hidden="true" />}
          </li>
        );
      })}
    </ol>
  );
}

/** Whether the operator got to this card by pressing something, rather than by loading the
 *  page onto it.
 *
 *  A context rather than a prop because every step renders its own `StepCard`, so a prop would
 *  be the same boolean threaded through four components that have no other use for it. The
 *  distinction is why the boolean exists at all: focusing a heading on a fresh load would steal
 *  focus from a page the operator has not read, while NOT focusing it after a press leaves them
 *  at the top of the document with the button they pressed gone. */
const StepMovedContext = createContext(false);

export function StepMovedProvider({ moved, children }: { moved: boolean; children: ReactNode }) {
  return <StepMovedContext.Provider value={moved}>{children}</StepMovedContext.Provider>;
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
  /** Already resolved by the caller (`t("setup.<step>.title")`); this component only lays it
   *  out. */
  title: string;
  children: ReactNode;
}) {
  const { t } = useTranslation();
  // Each step is a separate element, so moving between them unmounts one card and mounts the
  // next: the button that was pressed goes with it, and focus falls to <body>. Landing focus
  // on the new heading names the step for a screen reader and puts the keyboard back in the
  // card, and it is done here so all four steps get it from one place (rule 72). Reading the
  // title aloud is also why no announce() is added beside it: both would say the same words.
  const moved = useContext(StepMovedContext);
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    // Mount only, and only after a press. `scrollTo` in the wizard already put the card at the
    // top, so focusing must not fight it for the scroll position.
    if (moved) heading.current?.focus({ preventScroll: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <section className="step-card">
      <Stepper current={step} />
      <div className="step-head">
        <h2 ref={heading} tabIndex={-1}>
          {title}
        </h2>
        <span className="step-of">
          {t("setup.stepper.progress", { n: stepNumber(step), total: SETUP_STEPS.length })}
        </span>
      </div>
      {children}
    </section>
  );
}
