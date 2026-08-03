// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Freeze the page behind a full-screen overlay, so a touch drag scrolls the overlay's own
// overflow instead of the page underneath it. On iOS an unlocked body is what the drag
// scrolls, leaving a tall overlay impossible to reach the bottom of; worse, once the gesture
// commits to the body the overlay stops receiving it and reads as frozen. `position: fixed`
// (not merely `overflow: hidden`, which iOS ignores) is what actually holds; the scroll
// offset is parked and restored so the page does not jump.
//
// Ref-counted at module scope so an overlay opened over another (a modal over the why sheet)
// only releases the lock when the last one closes, and the first lock is the one that owns the
// saved offset. Both the modal shell and the review side-sheet share this one definition.

import { useEffect } from "react";

let scrollLocks = 0;
let lockedScrollY = 0;

export function lockPageScroll() {
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

export function unlockPageScroll() {
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

/** Hold the page still while `active` -- for the modal shell (always) and the review side
 *  sheet (only when it covers the whole screen on a phone). Toggling `active` locks and
 *  unlocks through the shared ref count above. */
export function usePageScrollLock(active: boolean) {
  useEffect(() => {
    if (!active) return;
    lockPageScroll();
    return unlockPageScroll;
  }, [active]);
}
