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
// schedule editor while a save is in flight) refuses the scrim, the ✕ and Escape by
// stating that once. The reap sheet is deliberately NOT a canClose user: since the reap
// went detached the run carries on in the ReapBar, so its sheet closes freely mid-run.
//
// One exception, named here so it cannot go quiet: the login screen's local-account sheet
// (LocalSheet in Login.tsx) keeps its own markup, because it stays mounted and slides up
// from the bottom rather than appearing over a scrim. It is not a second implementation of
// the contract -- it imports `trapTab` from here, so Tab containment has one definition.

import { useEffect, useId, useRef, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { useModalLayer } from "../backnav";
import { FOCUSABLE, useDialogFocus } from "../focus";
import { usePageScrollLock } from "../pageScrollLock";

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
  const { t } = useTranslation();
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
  // than partway down its controls. A modal is always the active case, so this passes no
  // flag; the `.why` panels share the hook and pass the media query, being a dialog only
  // on a phone (see `useDialogFocus`).
  useDialogFocus(panelRef);

  // Hold the page still behind the scrim, so scrolling stays inside the panel.
  usePageScrollLock(true);

  // Say out loud that a modal is up, so the list keyboard handlers behind the scrim can stand
  // down (useModalOpen). Declared here, in the one component every modal is built from, rather
  // than probed for as a `[role="dialog"]` element on each keypress (H-2).
  useModalLayer();

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

  // A click event fires on the nearest common ancestor of where the press began and where it
  // ended, so a drag that starts inside the panel and ends outside it dispatches `click` on the
  // SCRIM -- the panel never sees it, and its stopPropagation cannot help (B-17). Dragging
  // across the reap confirmation phrase to read or copy it tore the modal down, taking the
  // dry-run result and the typed phrase with it; the same gesture over a service form took the
  // typed credentials. So the scrim closes only on a press that BEGAN and ENDED on the scrim
  // itself, which is the only gesture that means "I clicked outside the panel".
  const pressBeganOnScrim = useRef(false);

  return (
    <div
      className="modal-scrim"
      onMouseDown={(e) => {
        pressBeganOnScrim.current = e.target === e.currentTarget;
      }}
      onMouseUp={(e) => {
        const outside = pressBeganOnScrim.current && e.target === e.currentTarget;
        pressBeganOnScrim.current = false;
        if (outside) close();
      }}
    >
      <div
        ref={panelRef}
        className={className ? `modal ${className}` : "modal"}
        role="dialog"
        aria-modal="true"
        aria-labelledby={headingId}
        tabIndex={-1}
        // The scrim no longer closes on a bubbled click, so this no longer guards against it.
        // It stays for what is above the scrim: these modals render inline in the React tree
        // (see the note at the top), so without it a click inside a panel would reach the click
        // handlers of whatever the modal happens to be rendered inside.
        onClick={(e) => e.stopPropagation()}
        onKeyDown={(e) => trapTab(e, panelRef.current)}
      >
        <header className="modal-head">
          <h2 id={headingId}>{title}</h2>
          <button
            className="icon-btn"
            onClick={close}
            disabled={!canClose}
            aria-label={t("common.close")}
          >
            ✕
          </button>
        </header>
        {children}
      </div>
    </div>
  );
}
