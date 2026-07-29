// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The one `.why` panel. Six surfaces render this shell -- the movie/season why panel, the show
// panel, the Scales person panel, the not-in-scan panel, and the loading/error fallbacks of the
// first and third -- and it owns what all six owe a keyboard operator, written once so a
// seventh cannot ship without it.
//
// It is a dialog wherever it covers the list and a side panel where it does not, and that is not
// a detail: index.css makes `main.split .why` a right-hand sheet floated over the cards at 1100px
// and `inset: 0; z-index: 50` at 900px, so from 1100px down this covers cards the operator can
// still reach, and under 900px it covers the entire application. Claiming `role="dialog"` at
// every width would be false above 1100px, where the panel really does sit beside the list in its
// own grid column and both are usable; not claiming it while it overlays leaves a screen reader
// browsing the covered page underneath. So the dialog contract is conditional on
// PANEL_OVERLAY_QUERY -- the 1100px boundary the CSS overlay block uses, read from the one
// declaration both sides share (rule 67).
//
// The boundary is 1100 and NOT `NARROW_SCREEN_QUERY`'s 900, which is the whole of #184: keyed on
// the 900, 200px of viewport width had the panel floating over the right of the cards with no
// focus move and no Tab trap, so a keyboard operator tabbed into cards hidden underneath and
// could press Spare or Reap on an item they could not see. The two numbers must stay apart --
// 900 is the stored meaning of the operator's Mobile/Desktop choice and cannot follow this one.
//
// Escape lives here too. It used to live in three places and be missing from a fourth: App's
// review-view key handler covered the two panels reachable from the queue, ScalesPanel and
// NotInScanPanel each carried their own copy, and ScalesPanelFallback carried none -- so a
// Scales panel sitting in its loading or error state could not be dismissed from the keyboard
// at all. Owning it here is what makes that unfixable-again (rule 72).

import { useEffect, useRef, type ReactNode } from "react";
import { useModalOpen } from "../backnav";
import { useDialogFocus } from "../focus";
import { PANEL_OVERLAY_QUERY, useMediaQuery } from "../useMediaQuery";
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
  const modal = useMediaQuery(PANEL_OVERLAY_QUERY);

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
      // Above the overlay boundary this is a plain <aside> (complementary), named but not modal.
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
