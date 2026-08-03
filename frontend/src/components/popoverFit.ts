// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Keeping an anchored popover on screen (rule 138).
//
// A popover left-aligned to its anchor runs off the right edge as soon as that anchor sits near
// the edge -- which on a phone is most of a toolbar row. Nothing catches it: the page does not
// scroll sideways, so whatever lands past the edge is not merely awkward, it is unreachable, and
// the operator is left choosing from the half of a menu they can see.
//
// The two popovers here (the review queue's filter menus, the policy editor's value suggestions)
// stay absolutely positioned inside their anchor, which is what keeps them glued to their control
// as the page scrolls. This slides them back into view on top of that. The account menu solves
// the same problem in CSS alone -- `.user-dropdown` is `right: 0`, growing leftward from a corner
// it never leaves -- and the spare-length menu (`OverrideControls.toggleMenu`) clamps its own
// `position: fixed` coordinates; neither needs this, and both are left alone.

import { useLayoutEffect, useState, type RefObject } from "react";

/** How far LEFT an anchor-aligned popover must slide to sit fully on screen, in pixels.
 *
 *  Never positive: a popover may be pulled back toward the left edge, never pushed right of the
 *  anchor it belongs to, which would cut the line between a control and the thing it opened.
 *
 *  The pull also stops at the near gutter. A popover too wide for the screen therefore keeps its
 *  LEFT edge and overflows on the right -- headings, tick marks and the start of every label
 *  stay put, and what spills is the end of the longest line. Sliding it the rest of the way would
 *  only move the same missing pixels to the side the operator reads from.
 */
export function popoverShift(
  anchorLeft: number,
  popoverWidth: number,
  viewportWidth: number,
  gutter = 8,
): number {
  // Where the popover's left edge wants to be, in viewport coordinates: at its anchor, but no
  // further right than the last position that still leaves all of it on screen, and no further
  // left than the gutter. `rightmostThatFits` goes NEGATIVE for a popover wider than the screen,
  // and the gutter floor is what then wins.
  const rightmostThatFits = viewportWidth - gutter - popoverWidth;
  const target = Math.min(anchorLeft, Math.max(gutter, rightmostThatFits));
  // Capping the target AT the anchor above is what makes this a pull left and never a push right,
  // so no clamp is needed here -- and a subtraction rather than a negation keeps a zero pull at 0
  // instead of the -0 that compares unequal to it.
  return target - anchorLeft;
}

/** Measures an open popover against the viewport and returns the pixels to slide it left, for the
 *  caller to hand to CSS as a custom property. Attach the ref to the popover itself; it finds its
 *  own anchor by class.
 *
 *  Measured after EVERY render, not once on open, because a popover's width is not fixed for as
 *  long as it is open: the suggestion list refilters on each keystroke. Re-measuring is idempotent
 *  -- the inputs are the ANCHOR's position and the popover's own width, and neither moves when the
 *  popover slides -- so an unchanged result bails out of the re-render instead of settling into a
 *  loop. React re-renders nothing on a window resize, hence the listener beside it. */
export function usePopoverShift(ref: RefObject<HTMLElement | null>, anchorClass: string): number {
  const [shift, setShift] = useState(0);

  // Layout, not paint: the slide has to land before the browser draws, or the popover appears in
  // the wrong place and jumps.
  useLayoutEffect(() => {
    const fit = () => {
      const el = ref.current;
      const anchor = el?.closest(`.${anchorClass}`);
      if (!el || !anchor) return;
      setShift(
        popoverShift(anchor.getBoundingClientRect().left, el.offsetWidth, window.innerWidth),
      );
    };
    fit();
    window.addEventListener("resize", fit);
    return () => window.removeEventListener("resize", fit);
  });

  return shift;
}
