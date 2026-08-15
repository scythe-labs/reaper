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
// it never leaves, and needs none of this.
//
// `useFixedMenu` below is the OTHER answer, for a popover that cannot be anchor-relative at all:
// the Spare length menu and the collection picker each sit inside a card whose `overflow: hidden`
// (backdrop art) would clip an absolutely positioned child, so both take `position: fixed`
// coordinates instead, clamped and flipped the same way this file's `popoverShift` slides its own
// popovers -- just measured once, on open, rather than resliding on every render.

import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type MouseEvent as ReactMouseEvent,
  type RefObject,
} from "react";
import { useBackGuard } from "../backnav";

/** Where a `position: fixed` popover sits: always `left`, plus exactly one vertical anchor --
 *  `top` when it opens below its trigger, `bottom` when it flips above (so the popover stays
 *  snug to the trigger whatever its real rendered height is, rather than a `top` computed from a
 *  guessed one). */
export type MenuPos = { left: number; top?: number; bottom?: number };

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

/** Where a `position: fixed` popover belongs, anchored to a trigger's bounding rect: right-
 *  aligned to it and clamped to the viewport horizontally, flipped above the trigger when it
 *  would otherwise run off the bottom.
 *
 *  `height` is only ever an upper bound for that flip decision, never a drawn coordinate: below,
 *  the popover's TOP is pinned to the anchor's bottom edge; above, its BOTTOM is pinned just over
 *  the anchor, so it stays snug to the trigger whatever its real rendered height turns out to be
 *  -- a `top` computed from a guessed height would float the popover off the button once the
 *  real content came in shorter than the guess. */
export function fixedMenuPos(
  anchor: { left: number; right: number; top: number; bottom: number },
  width: number,
  height: number,
  viewportWidth: number,
  viewportHeight: number,
  gutter = 8,
): MenuPos {
  const left = Math.max(gutter, Math.min(anchor.right - width, viewportWidth - width - gutter));
  const below = anchor.bottom + height <= viewportHeight;
  return below
    ? { left, top: anchor.bottom + 4 }
    : { left, bottom: viewportHeight - anchor.top + 4 };
}

/** A `position: fixed` popover anchored to a trigger element (the Spare length menu's caret, the
 *  collection picker's caret): open/closed state, the clamped position, and every way to dismiss
 *  it, in one hook -- `OverrideControls.toggleMenu` and `CollectionChip.toggleOpen` each
 *  reimplemented this shape until #816 (rule 138, rule 72).
 *
 *  `anchorRef` is the trigger the popover measures itself against on open, and treats as part of
 *  itself for the outside-click check (a click on the caret that opened the menu must toggle it,
 *  never close-then-reopen). `width`/`height` feed `fixedMenuPos`. `ignoreScrollRef`, when
 *  supplied, lets the caller suppress a scroll-close while it reads true -- the Spare menu's
 *  Custom-length box sets this while it is open, so a phone's virtual keyboard opening (which
 *  scrolls the viewport to reveal the focused field) cannot dismiss the menu before a digit is
 *  typed.
 *
 *  Returns `menuRef` for the caller to attach to the portaled popover root -- it doubles as the
 *  outside-click boundary and the `useDialogFocus`/`trapTab` target -- plus `pos` (null while
 *  closed), `toggle` for the trigger's `onClick`, and `close`. */
export function useFixedMenu<T extends HTMLElement = HTMLDivElement>(
  anchorRef: RefObject<HTMLElement | null>,
  {
    width,
    height,
    ignoreScrollRef,
  }: { width: number; height: number; ignoreScrollRef?: RefObject<boolean> },
): {
  pos: MenuPos | null;
  menuRef: RefObject<T | null>;
  toggle: (e: ReactMouseEvent) => void;
  close: () => void;
} {
  const [pos, setPos] = useState<MenuPos | null>(null);
  const menuRef = useRef<T>(null);

  const close = useCallback(() => setPos(null), []);
  useBackGuard(pos !== null, close);

  const toggle = (e: ReactMouseEvent) => {
    e.stopPropagation();
    if (pos) {
      close();
      return;
    }
    const btn = anchorRef.current;
    if (!btn) return;
    setPos(
      fixedMenuPos(
        btn.getBoundingClientRect(),
        width,
        height,
        window.innerWidth,
        window.innerHeight,
      ),
    );
  };

  // Read fresh inside the listeners without re-subscribing them on every render (rule 19).
  const closeRef = useRef(close);
  closeRef.current = close;
  useEffect(() => {
    if (!pos) return;
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node;
      if (!menuRef.current?.contains(t) && !anchorRef.current?.contains(t)) closeRef.current();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      // The newest layer consumes Escape, so it never also closes a panel underneath (rule 72:
      // the filter popovers in ReviewQueue stop the same key for the same reason).
      e.stopPropagation();
      closeRef.current();
    };
    const onScroll = () => {
      if (ignoreScrollRef?.current) return;
      closeRef.current();
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    window.addEventListener("scroll", onScroll, true);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("scroll", onScroll, true);
    };
  }, [pos, anchorRef, ignoreScrollRef]);

  return { pos, menuRef, toggle, close };
}
