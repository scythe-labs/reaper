// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Where focus goes when a surface opens and when it closes, written once because the app kept
// answering it per site and kept forgetting one: an app-wide audit found six `.why` panels and
// three menus that never answered it at all, against six `.focus()` calls in the whole frontend.
//
// The failure is always the same shape and always invisible to a sighted mouse user, which is
// why it survived so long: `document.activeElement` falls back to `<body>`, and the next Tab
// restarts at the top of the document.

import { useEffect, useRef, type RefObject } from "react";

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
 *  them where they are.
 *
 *  `entry` says which element inside the surface takes focus, for the surfaces whose container
 *  cannot take it. On iOS a `role="group"` WITH CHILDREN is never a `UIAccessibilityElement`
 *  (WebKit's `determineIsAccessibilityElement`, and a committed layout test), so focusing the
 *  spare-length menu's container put the VoiceOver cursor nowhere and a swipe walked the panel
 *  BEHIND the menu instead of its own rows (#206). `tabIndex={-1}` is not what fails there, and
 *  neither is the portal, so the fix is to land on a real control -- the APG Menu Button pattern.
 *  Default is the container, which is right for a dialog and is what the other two callers keep.
 *  The close-restore is unaffected either way: `entry` is inside the panel, so the `contains`
 *  check below still recognizes the surface as holding focus. */
export function useDialogFocus(
  panelRef: RefObject<HTMLElement | null>,
  active = true,
  entry?: RefObject<HTMLElement | null>,
) {
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
    // Falls back to the panel when the named entry is not on screen, so a surface whose entry is
    // conditional still gets focus rather than leaving it on `<body>`.
    if (active) (entry?.current ?? panelRef.current)?.focus();
  }, [active, panelRef, entry]);
}

/** Everything a browser will put in the Tab order, in document order.
 *
 *  Lives here rather than beside the modal that first needed it, so that anything else needing
 *  the same list takes this one instead of writing a second (rule 18). `trapTab` in ModalShell
 *  is its only reader today. */
export const FOCUSABLE =
  "a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), " +
  'textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/** The marker a control wears when it removes the row it lives in, so `useRemovalFocus` can
 *  find the row that took its place.
 *
 *  An attribute rather than a CSS class or a walk over the markup's shape: the three lists this
 *  covers are a chip, a filter pill and a rule row, whose containers agree about nothing, and a
 *  selector guessing at structure is a selector that silently matches zero elements the next
 *  time a row grows a second button. It is also greppable, which a structural walk is not. */
export const REMOVES_ITS_ROW = { "data-removes-row": "" } as const;

/** Keep a keyboard operator where they were when a control removes the thing containing it.
 *
 *  Activating such a control destroys the focused element, so focus falls to `<body>` and the
 *  next Tab restarts at the top of the document -- on the policy page, a ~1,900-line form. An
 *  operator removing three tags is thrown to the top three times (#173).
 *
 *  Used as: spread `REMOVES_ITS_ROW` onto every remove control in the list, put `ref` on their
 *  common container, and call `removing(i)` in the same handler that removes row `i`.
 *
 *      const rows = useRemovalFocus(addBoxRef);
 *      <div ref={rows.ref}>
 *        {items.map((it, i) => (
 *          <button {...REMOVES_ITS_ROW} onClick={() => { rows.removing(i); drop(i); }} />
 *        ))}
 *
 *  Focus lands on the control that now occupies that index -- the NEXT row, which is what the
 *  eye expects after a delete -- clamping to the last one when the row removed was the last.
 *  When the list empties there is no row to go to, so it falls back to `fallbackRef`: the box
 *  that ADDS an item, which is both a stable neighbour and the only thing left to do there.
 *
 *  The move waits for a commit rather than running inline in the handler. The row is still in
 *  the DOM when the handler runs, so an inline `.focus()` would land on the element that is
 *  about to be removed and the browser would drop it to `<body>` anyway -- the same silent
 *  no-op as focusing a `disabled` control (`OverrideControls`' `returnToCaret` hit that one).
 *  `ModalShell.tsx:89-96` is the same pattern for the open/close case. */
export function useRemovalFocus(fallbackRef?: RefObject<HTMLElement | null>) {
  const ref = useRef<HTMLElement>(null);
  const wanted = useRef<number | null>(null);
  useEffect(() => {
    const at = wanted.current;
    if (at === null) return;
    wanted.current = null;
    const rows = [...(ref.current?.querySelectorAll<HTMLElement>("[data-removes-row]") ?? [])];
    const next = at < rows.length ? rows[at] : rows[rows.length - 1];
    (next ?? fallbackRef?.current)?.focus();
  });
  return {
    ref,
    /** Say which row is about to go, in the handler that removes it. */
    removing: (index: number) => {
      wanted.current = index;
    },
  };
}

/** A control nobody can act on, so neither a focus TARGET nor a place worth leaving someone.
 *  Focusing one is a silent no-op -- the browser drops it straight back to `<body>`, which is the
 *  trap `OverrideControls`' `returnToCaret` hit. Read from both ends by `useSuccessorFocus`. */
const UNACTABLE = "[disabled], [aria-disabled='true']";

/** Where focus goes when the control that was pressed goes with the thing it acted on, and its
 *  replacement arrives a server round trip later.
 *
 *  `useRemovalFocus` above cannot cover these: it resolves the target on the very next commit,
 *  which is the right shape for a draft list edited in the browser and the wrong one for anything
 *  waiting on a write. Six of the seven sites this was written for also carry
 *  `disabled={…isPending}`, so the browser has already dropped focus to `<body>` at the press,
 *  BEFORE the unmount -- there is nothing left to move by the time the successor exists (#173).
 *
 *  So the request is armed in the handler and every commit afterwards gets a turn at satisfying
 *  it. Three settle timings fall out of one hook: a successor that mounts with `onSuccess`, one
 *  that waits for the invalidated query to refetch, and one that passes through an intermediate
 *  state where the only candidate is still `disabled` -- which is why being mounted is not
 *  enough, and a disabled target is waited out rather than focused. Focusing a disabled control
 *  is the silent no-op `OverrideControls`' `returnToCaret` hit.
 *
 *  Used as:
 *
 *      const after = useSuccessorFocus();
 *      const remove = useMutation({ …, onSuccess: () => { announce("…"); invalidate(); } });
 *      <button onClick={() => { after.arriving(); remove.mutate(); }} />
 *      <button ref={after.ref}>What is left to do here</button>
 *
 *  **Only while focus is still lost**, which is the difference between a recovery and a steal.
 *  If the operator has put focus somewhere themselves during the round trip, the request is
 *  dropped: a write settling under them must not pull them out of the box they are typing in.
 *  That check is also what keeps a request harmless when the successor never arrives at all --
 *  a failed refetch leaves it armed, and the next commit that finds focus lost is the only one
 *  that can spend it.
 *
 *  "Lost" is broader than `<body>`, and deliberately so. A browser blurs an element that becomes
 *  `disabled`, so on the sites whose press disables the control it is on, `<body>` is what a real
 *  browser reports -- but jsdom does NOT blur it, and leaves the cursor parked on a control that
 *  can no longer be acted on. Reading only `<body>` would make this hook behave one way in the
 *  suite and another in the app, which is the worst of both. A control that is gone, or still
 *  there and no longer actable, is nowhere to stand either way. */
export function useSuccessorFocus() {
  const ref = useRef<HTMLElement>(null);
  const wanted = useRef(false);
  useEffect(() => {
    if (!wanted.current) return;
    const target = ref.current;
    // Not mounted yet, or mounted but not actable yet: keep the request and take the next commit.
    if (target === null || !target.isConnected) return;
    if (target.matches(UNACTABLE)) return;
    const at = document.activeElement;
    const lost =
      at === null ||
      at === document.body ||
      !at.isConnected ||
      (at instanceof HTMLElement && at.matches(UNACTABLE));
    wanted.current = false;
    if (lost) target.focus();
  });
  return {
    ref,
    /** Say a successor is on its way, in the handler that presses the control. */
    arriving: () => {
      wanted.current = true;
    },
  };
}

/** Where focus goes when a savebar unmounts, taking the button that was pressed.
 *
 *  A savebar exists only while something is unsaved, so Save and Discard both destroy the
 *  control the operator just activated: focus falls to `<body>` and the next Tab restarts above
 *  the whole form -- the policy page's is ~1,900 lines (#173). Both presses already ANNOUNCE
 *  ("Movie policy saved.", "Changes discarded."), so what is missing is only the place to stand.
 *
 *  That place is the panel's own heading, and it is chosen for what it is rather than for being
 *  nearby: after a completed action a screen-reader operator wants the name of what they are
 *  looking at, and it is a fixed, findable point rather than wherever the bar happened to be.
 *  Pass the heading a `tabIndex={-1}` so it can hold focus without joining the Tab order.
 *
 *  Returns `[headingRef, afterSave]`. Call `afterSave()` in the same handler as the press. The
 *  move waits for a commit, because the heading is only reachable once the bar it replaced has
 *  gone -- and on the save path the bar is still up while the write is in flight. */
export function useSavebarFocus() {
  const headingRef = useRef<HTMLElement>(null);
  const wanted = useRef(false);
  useEffect(() => {
    if (!wanted.current) return;
    // Only once the bar is actually gone. A save keeps it up while the write is in flight, and
    // focusing the heading then would move the operator off a Discard they can still press.
    if (document.body.contains(headingRef.current) && !document.querySelector(".savebar")) {
      wanted.current = false;
      headingRef.current?.focus();
    }
  });
  return {
    ref: headingRef,
    /** Say that the bar is on its way out, in the handler that dismisses it. */
    leaving: () => {
      wanted.current = true;
    },
  };
}
