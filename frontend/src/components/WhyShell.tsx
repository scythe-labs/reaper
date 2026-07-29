// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The one `.why` panel. Six surfaces render this shell -- the movie/season why panel, the show
// panel, the Scales person panel, the not-in-scan panel, and the loading/error fallbacks of the
// first and third -- and it owns what all six owe a keyboard operator, written once so a
// seventh cannot ship without it.
//
// It is a dialog on a phone and a side panel on a desktop, and that is not a detail: index.css
// makes `main.split .why` a right-hand sheet at 1100px and `inset: 0; z-index: 50` at 900px, so
// under 900px this covers the entire application. Claiming `role="dialog"` at every width would
// be false above 1100px, where the panel really does sit beside the list and both are usable;
// not claiming it on a phone leaves a screen reader browsing the covered page underneath. So the
// dialog contract is conditional on NARROW_SCREEN_QUERY -- the same boundary the CSS uses, read
// from the one declaration both sides share (rule 67).
//
// KNOWN GAP, issue #184: those two widths are not the same number. The panel starts overlaying
// the list at 1100px, not 900px, so between 901px and 1100px it floats over the right of the
// cards while this treats it as a side panel -- no focus move, no Tab trap, and the covered cards
// still in the Tab order with their Spare and Reap. Do not read the paragraph above as saying
// that band is handled; it is #171 surviving in 200px of width. The fix is a second constant for
// the 1100px block rather than moving this one, whose 900 is the stored meaning of the operator's
// Mobile/Desktop choice.
//
// Escape lives here too. It used to live in three places and be missing from a fourth: App's
// review-view key handler covered the two panels reachable from the queue, ScalesPanel and
// NotInScanPanel each carried their own copy, and ScalesPanelFallback carried none -- so a
// Scales panel sitting in its loading or error state could not be dismissed from the keyboard
// at all. Owning it here is what makes that unfixable-again (rule 72).

import { useEffect, useRef, type ReactNode } from "react";
import { useModalOpen } from "../backnav";
import { useDialogFocus } from "../focus";
import { NARROW_SCREEN_QUERY, useMediaQuery } from "../useMediaQuery";
import { trapTab } from "./ModalShell";

/** Every `.why` panel's close: a media-sheet disc floated over the hero art (see .why-close),
 *  never a boxed glyph in the title row, where it read as a stray form control. A stroked X to
 *  match the panel's round-capped glyphs, and NOT the scythe: closing is not reaping. Over a
 *  title with no art it rests on the surface top-right the same way. Rendered FIRST by the shell
 *  so it is the panel's first tab stop; z-index clears the hero art and fade. */
function WhyClose({ onClose }: { onClose: () => void }) {
  return (
    <button type="button" className="why-close" onClick={onClose} aria-label="Close">
      <svg viewBox="0 0 16 16" width="15" height="15" fill="none" aria-hidden="true">
        <path
          d="M4 4l8 8M12 4l-8 8"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinecap="round"
        />
      </svg>
    </button>
  );
}

export function WhyShell({
  headingId,
  onClose,
  children,
}: {
  /** The id the panel's own `<h2>` carries. That heading IS the panel's accessible name, so it
   *  is pointed at rather than copied into an `aria-label`: a duplicate string is one more
   *  place for the spoken name and the visible one to drift apart (rule 144). Each of the six
   *  is pinned to its name by a test -- six hand-written tests, one per panel, which is a
   *  record of the six and NOT a gate on a seventh: nothing fails if a new call site passes an
   *  id it never renders, and the panel is then unnamed on a phone. Turning this into a gate
   *  means enumerating the `<WhyShell` call sites and asserting each has such a test, the shape
   *  `test_repo_hygiene.py` uses on the other side of the tree (rule 145). */
  headingId: string;
  onClose: () => void;
  children: ReactNode;
}) {
  const panelRef = useRef<HTMLElement>(null);
  const modal = useMediaQuery(NARROW_SCREEN_QUERY);

  useDialogFocus(panelRef, modal);

  // Escape closes. A modal, if one is up, owns the key first.
  //
  // The listener is on `window`, so it hears every press on the page, and Escape already means
  // something inside a text box: it clears a `type="search"` field natively. The queue's search
  // box sits beside this panel in split view, so an unscoped handler closed the reasoning the
  // operator was reading when they only meant to clear their search. App's old handler bailed on
  // INPUT/TEXTAREA/SELECT for exactly that reason and this one keeps the bail -- scoped, so a
  // field the panel itself owns still closes it. (None of the six render one today; the scope is
  // what lets a seventh, and what keeps this honest about which presses are ours.)
  const modalOpen = useModalOpen();
  const closeRef = useRef(onClose);
  useEffect(() => {
    closeRef.current = onClose;
  });
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || e.metaKey || e.ctrlKey || e.altKey) return;
      if (modalOpen) return;
      const target = e.target as HTMLElement | null;
      const tag = target?.tagName;
      const inAField =
        tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || target?.isContentEditable;
      if (inAField && !panelRef.current?.contains(target)) return;
      closeRef.current();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [modalOpen]);

  return (
    <aside
      ref={panelRef}
      className="why"
      // A wide screen leaves this a plain <aside> (complementary), named but not modal.
      role={modal ? "dialog" : undefined}
      aria-modal={modal || undefined}
      aria-labelledby={headingId}
      tabIndex={-1}
      onKeyDown={modal ? (e) => trapTab(e, panelRef.current) : undefined}
    >
      <WhyClose onClose={onClose} />
      {children}
    </aside>
  );
}
