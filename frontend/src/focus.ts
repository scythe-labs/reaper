// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Where focus goes when a surface opens and when it closes, written once because the app kept
// answering it per site and kept forgetting one: an app-wide audit found six `.why` panels and
// three menus that never answered it at all, against six `.focus()` calls in the whole frontend.
//
// The failure is always the same shape and always invisible to a sighted mouse user, which is
// why it survived so long: `document.activeElement` falls back to `<body>`, and the next Tab
// restarts at the top of the document.

import { useEffect, type RefObject } from "react";

/** Move focus into a surface on open and hand it back on close.
 *
 *  `active` is what makes this reusable across the two kinds of surface that need it. A modal
 *  is always active. The `.why` panels are a split-view side panel on a wide screen and a
 *  full-screen sheet on a phone, so they pass the media query: on a desktop the panel must NOT
 *  steal focus, because it opens beside the list rather than over it.
 *
 *  Handing focus back is deliberately conditional, and that condition is the difference between
 *  a restore and a yank. Focus returns to the invoker only if this surface still holds it (or
 *  it has already fallen to `<body>`, which is what an unmount looks like). A panel that never
 *  took focus, on a desktop, closing while the operator is typing somewhere else, must leave
 *  them where they are. */
export function useDialogFocus(panelRef: RefObject<HTMLElement | null>, active = true) {
  // The two jobs are split across two effects on purpose, and the ORDER matters both ways.
  //
  // This one is declared first so it reads `document.activeElement` before the second can move
  // it: capture the invoker after the panel has taken focus and the invoker IS the panel, so
  // closing would hand focus to a node that is going away.
  //
  // Its deps are `[]` (bar the stable ref) because this cleanup means CLOSED. Keying it on
  // `active` as well ran the close-restore whenever the flag merely flipped -- the viewport
  // crossing the panel's media query, which is a phone being rotated -- and yanked focus out of
  // a panel that was still on screen, back to the card behind it.
  useEffect(() => {
    // Read the ref into a local: on unmount React may detach it before this cleanup runs, and
    // the cleanup needs to know whether focus was still inside the panel that is going away.
    const panel = panelRef.current;
    const invoker = document.activeElement;
    return () => {
      if (!(invoker instanceof HTMLElement) || !invoker.isConnected) return;
      const held =
        document.activeElement === document.body ||
        document.activeElement === null ||
        (panel != null && panel.contains(document.activeElement));
      if (held) invoker.focus();
    };
  }, [panelRef]);

  // Focus in, and re-enter if the surface becomes active later: a panel the operator opened on a
  // wide screen and then narrowed into a full-screen sheet is covering the app from that moment,
  // so it takes focus from that moment. Going the other way it simply stops trapping and leaves
  // focus where the operator has it -- there is nothing to hand back to while it is still open.
  useEffect(() => {
    if (active) panelRef.current?.focus();
  }, [active, panelRef]);
}

/** Everything a browser will put in the Tab order, in document order.
 *
 *  Lives here rather than beside the modal that first needed it, so that anything else needing
 *  the same list takes this one instead of writing a second (rule 18). `trapTab` in ModalShell
 *  is its only reader today. */
export const FOCUSABLE =
  "a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), " +
  'textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
