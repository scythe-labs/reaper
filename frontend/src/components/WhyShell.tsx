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
// be false on a wide screen, where the panel sits beside the list and both are usable; not
// claiming it on a phone leaves a screen reader browsing the covered page underneath. So the
// dialog contract is conditional on NARROW_SCREEN_QUERY -- the same boundary the CSS uses, read
// from the one declaration both sides share (rule 67).
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
   *  place for the spoken name and the visible one to drift apart (rule 144). Every panel is
   *  pinned to its name by a test, which is what stops a seventh from passing an id it never
   *  renders. */
  headingId: string;
  onClose: () => void;
  children: ReactNode;
}) {
  const panelRef = useRef<HTMLElement>(null);
  const modal = useMediaQuery(NARROW_SCREEN_QUERY);

  useDialogFocus(panelRef, modal);

  // Escape closes. A modal, if one is up, owns the key first -- and unlike App's old handler
  // this does NOT bail when the press comes from a text box, because the panel contains its own
  // fields and Escape from inside one used to do nothing at all.
  const modalOpen = useModalOpen();
  const closeRef = useRef(onClose);
  useEffect(() => {
    closeRef.current = onClose;
  });
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || e.metaKey || e.ctrlKey || e.altKey) return;
      if (modalOpen) return;
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
