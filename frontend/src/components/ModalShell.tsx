// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The one modal. Every modal in the app is this shell: the scrim, the panel, and a
// header with its close button, plus everything a modal owes a keyboard user, written
// once so no modal can ship without it.
//
// Two things it owns that the hand-built copies never had:
//   1. Dialog semantics: role="dialog", aria-modal, and a name taken from the heading,
//      so a screen reader announces what just opened instead of a bare group of controls.
//   2. Focus. Focus moves into the panel on open, Tab stays inside it (the page behind
//      the scrim is not reachable while it is up), and focus returns to whatever opened
//      the modal on close. Tab is kept in by a small trap rather than `inert` on the app
//      root, because these modals render inline in the React tree, not through a portal:
//      marking the root inert would mark the modal inert with it.
//
// Closing is routed through one `canClose` guard, so a modal that must stay open (the
// reap sheet while a real reap is in flight) refuses the scrim, the ✕ and Escape by
// stating that once.
//
// One exception, named here so it cannot go quiet: the login screen's local-account sheet
// (LocalSheet in Login.tsx) keeps its own markup, because it stays mounted and slides up
// from the bottom rather than appearing over a scrim. It is not a second implementation of
// the contract -- it imports `trapTab` from here, so Tab containment has one definition.

import { useEffect, useId, useRef, type ReactNode } from "react";

/** Everything a browser will put in the Tab order, in document order. */
export const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), ' +
  'textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/** Freeze the page behind the scrim while a modal is up, so a touch drag scrolls the
 *  panel's own overflow instead of the page underneath it -- on iOS an unlocked body is
 *  what the drag scrolls, leaving a tall modal (the service editor) impossible to reach the
 *  bottom of. `position: fixed` (not merely `overflow: hidden`, which iOS ignores) is what
 *  actually holds; the scroll offset is parked and restored so the page does not jump.
 *
 *  Ref-counted at module scope so a modal opened over another (rare here) only releases the
 *  lock when the last one closes, and the first lock is the one that owns the saved offset. */
let scrollLocks = 0;
let lockedScrollY = 0;
function lockPageScroll() {
  if (scrollLocks === 0) {
    lockedScrollY = window.scrollY;
    const { style } = document.body;
    style.position = "fixed";
    style.top = `-${lockedScrollY}px`;
    style.left = "0";
    style.right = "0";
    style.width = "100%";
  }
  scrollLocks += 1;
}
function unlockPageScroll() {
  scrollLocks -= 1;
  if (scrollLocks === 0) {
    const { style } = document.body;
    style.position = "";
    style.top = "";
    style.left = "";
    style.right = "";
    style.width = "";
    window.scrollTo(0, lockedScrollY);
  }
}

/** Tab containment for one dialog panel: wrap from the last control back to the first and
 *  vice versa, so the still-rendered page behind it never takes focus.
 *
 *  Exported because the login sheet owns its own markup (see the note at the top of this
 *  file) and must not grow a second copy of this. Pass the panel element; a null panel is
 *  a no-op, which is what a closed sheet wants. */
export function trapTab(e: React.KeyboardEvent, panel: HTMLElement | null) {
  if (e.key !== "Tab" || !panel) return;
  const items = Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE));
  const first = items[0];
  const last = items[items.length - 1];
  if (!first || !last) {
    e.preventDefault(); // nothing to focus: keep the page behind the scrim out of reach
    return;
  }
  const active = document.activeElement;
  if (e.shiftKey && (active === first || active === panel)) {
    e.preventDefault();
    last.focus();
  } else if (!e.shiftKey && active === last) {
    e.preventDefault();
    first.focus();
  }
}

export function ModalShell({
  title,
  onClose,
  canClose = true,
  className,
  children,
}: {
  /** The heading. It also becomes the dialog's accessible name. */
  title: ReactNode;
  onClose: () => void;
  /** False while the modal must stay open: the scrim, Escape and the ✕ all refuse. */
  canClose?: boolean;
  /** Extra class on the panel, for per-modal layout. */
  className?: string;
  children: ReactNode;
}) {
  const headingId = useId();
  const panelRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef(onClose);
  const canCloseRef = useRef(canClose);

  // Kept in refs so the Escape listener below can be registered once and still read the
  // current guard: callers pass a fresh onClose on every render.
  useEffect(() => {
    closeRef.current = onClose;
    canCloseRef.current = canClose;
  });

  // Focus in on open, and back to whatever opened us on close. The panel itself takes
  // the initial focus (tabIndex -1) so the reading starts at the dialog's name rather
  // than partway down its controls.
  useEffect(() => {
    const invoker = document.activeElement;
    panelRef.current?.focus();
    return () => {
      if (invoker instanceof HTMLElement && invoker.isConnected) invoker.focus();
    };
  }, []);

  // Hold the page still behind the scrim, so scrolling stays inside the panel.
  useEffect(() => {
    lockPageScroll();
    return unlockPageScroll;
  }, []);

  // Escape closes, through the same guard as the scrim and the ✕.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && canCloseRef.current) closeRef.current();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const close = () => {
    if (canClose) onClose();
  };

  return (
    <div className="modal-scrim" onClick={close}>
      <div
        ref={panelRef}
        className={className ? `modal ${className}` : "modal"}
        role="dialog"
        aria-modal="true"
        aria-labelledby={headingId}
        tabIndex={-1}
        onClick={(e) => e.stopPropagation()}
        onKeyDown={(e) => trapTab(e, panelRef.current)}
      >
        <header className="modal-head">
          <h2 id={headingId}>{title}</h2>
          <button className="icon-btn" onClick={close} disabled={!canClose} aria-label="Close">
            ✕
          </button>
        </header>
        {children}
      </div>
    </div>
  );
}
