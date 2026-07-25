// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Make the browser Back button step *back through the UI* instead of leaving Reaper.
//
// The app has no router: where you are (which tab, which open panel, which menu) is plain
// React state, so a Back press used to navigate away from the whole page. This controller maps
// "somewhere to go back to" onto browser history -- closing the topmost open thing first, and
// only leaving the app once nothing is left open.
//
// The model is ONE parked history entry per open layer. N open layers cost N browser entries
// and take N Back presses to unwind, newest-first.
//
// It was one shared entry for all layers, which unwound identically and cost the browser less --
// but iOS keeps a back-forward SNAPSHOT per history entry, captured when the page navigates away
// from that entry, and paints it during an edge-swipe back. One shared entry meant the picture
// came from whenever the FIRST layer opened: open a tab, then scroll deep and open a card, and
// the swipe back painted the list as it looked at the tab change -- scrolled to the top -- and
// froze there for the seconds WebKit takes to drop a snapshot. The page underneath was correct
// the whole time, which is why closing with the panel's own X always looked right. Parking an
// entry per layer gives each layer a snapshot of the page as it looked when that layer opened,
// which is exactly what Back reveals.
//
// Two kinds of "layer", unwound newest-first:
//   - overlays (menus, modals, side panels): registered via `useBackGuard(open, close)` while
//     open, and auto-removed when they close by ANY means (Escape, the X, an outside click).
//   - navigation frames (a tab / section change): recorded via `pushNav(undo)`; a Back runs the
//     undo to restore the previous location. These persist until a Back consumes them.

import {
  createContext,
  useContext,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";

/** One thing a Back press can unwind. `onBack` closes an overlay or restores a prior tab. */
type Layer = { id: number; onBack: () => void };

type BackNavApi = {
  /** Register an open overlay; returns its id. Call `remove(id)` when it closes. */
  register: (onBack: () => void) => number;
  remove: (id: number) => void;
  /** Record a tab / section change so Back restores the previous one. */
  pushNav: (undo: () => void) => void;
  /** Count one open modal; the returned function uncounts it. See `useModalOpen`. */
  enterModal: () => () => void;
};

const BackNavContext = createContext<BackNavApi | null>(null);

/** How many modals are open. Separate from the layer stack above, which also holds menus,
 *  side panels and tab changes -- none of which take the keyboard away from the list. */
const ModalDepthContext = createContext(0);

// The history entry we park. Marked so a stray popstate from elsewhere is still safe to handle.
const SENTINEL = { __reaperBack: true } as const;

export function BackNavProvider({ children }: { children: ReactNode }) {
  // A ref, not state: this controller renders nothing, and the popstate handler needs the live
  // list synchronously. Newest layer last.
  const layersRef = useRef<Layer[]>([]);
  const seqRef = useRef(0);
  // How many sentinel entries we have parked -- one per open layer.
  const parkedRef = useRef(0);
  // How many popstates WE caused (history.back() to un-park a layer that closed by non-Back
  // means) and must swallow rather than treat as a user Back. A count, not a flag: two layers
  // can close in one tick, and a flag would let the second popstate through as a real Back.
  const selfPopRef = useRef(0);
  // Parked sentinel entries survive a page reload (their pushState state persists), but the
  // counter above resets to 0 on the fresh mount -- so on reload we would be sitting on stale
  // sentinels with no layers behind them, and the first Back presses would be dead. Reconciled
  // once at mount (below); StrictMode double-invokes effects, so this guards it to a single run.
  const reconciledRef = useRef(false);

  // State, not a ref, because things RENDER off it (see useModalOpen). It costs no re-render of
  // the app: `children` arrives as an already-built element, so React skips that subtree when
  // this component re-renders, and only the hook's consumers update.
  const [modalDepth, setModalDepth] = useState(0);

  // Built once (lazily), not per render: every method touches only refs and globals, so a single
  // stable instance is correct and keeps the context value from changing.
  const apiRef = useRef<BackNavApi | null>(null);
  if (apiRef.current === null) {
    // One entry per layer, pushed as the layer opens -- which is also when the browser takes the
    // snapshot it will paint during an edge-swipe back (see the note at the top of this file).
    const park = () => {
      history.pushState(SENTINEL, "");
      parkedRef.current += 1;
    };
    // Give this layer's entry back by walking history back one step. The popstate it triggers is
    // swallowed via selfPopRef.
    const unpark = () => {
      if (parkedRef.current === 0) return;
      parkedRef.current -= 1;
      selfPopRef.current += 1;
      history.back();
    };
    apiRef.current = {
      register(onBack) {
        const id = ++seqRef.current;
        layersRef.current = [...layersRef.current, { id, onBack }];
        park(); // this layer's own entry, even if others are already parked
        return id;
      },
      remove(id) {
        const before = layersRef.current.length;
        layersRef.current = layersRef.current.filter((l) => l.id !== id);
        // Only the layer's own non-Back close reaches here for a still-present id. A Back press
        // slices the layer off first (below), so this is a no-op then -- no double-unpark.
        if (layersRef.current.length === before) return;
        // Give back this layer's entry, whether or not others remain open: each layer owns one.
        unpark();
      },
      pushNav(undo) {
        const id = ++seqRef.current;
        layersRef.current = [...layersRef.current, { id, onBack: undo }];
        park();
      },
      enterModal() {
        // The setter from useState is stable for the life of the component, so capturing the
        // first one here is safe. Counted, not a boolean: modals can stack.
        setModalDepth((n) => n + 1);
        return () => setModalDepth((n) => n - 1);
      },
    };
  }
  const api = apiRef.current;

  useEffect(() => {
    // Own scroll restoration. This app has no router, so it parks and un-parks a history
    // sentinel itself (pushState when a panel opens, history.back() when it closes). Under the
    // browser default `auto`, the engine tries to manage scroll across those history writes and,
    // with the card list's CSS containment (`container-type` on `.card-list`), lands the page at
    // the top on both the open and the close -- desktop and phone alike. `manual` hands the app
    // the scroll: parking and un-parking the sentinel no longer moves the reviewer, so they stay
    // exactly where they tapped. Restored on unmount so a test or an embedding host is left as it
    // was found.
    const priorScrollRestoration =
      "scrollRestoration" in history ? history.scrollRestoration : null;
    if (priorScrollRestoration !== null) history.scrollRestoration = "manual";

    const onPop = () => {
      if (selfPopRef.current > 0) {
        // Our own unpark(); that entry is gone, nothing to unwind.
        selfPopRef.current -= 1;
        return;
      }
      const layers = layersRef.current;
      const top = layers[layers.length - 1];
      if (!top) {
        // Nothing open: the browser popped past our (absent) sentinel and is leaving. Let it.
        parkedRef.current = 0;
        return;
      }
      // The browser just consumed the top layer's entry. Take that layer off; the layers still
      // open keep their own entries, so the next Back keeps unwinding rather than leaving.
      layersRef.current = layers.slice(0, -1);
      if (parkedRef.current > 0) parkedRef.current -= 1;
      top.onBack();
    };
    window.addEventListener("popstate", onPop);
    // Reconcile a sentinel left parked before a reload (see reconciledRef). Done after the
    // listener is attached so our own history.back() below is swallowed by selfPopRef, and only
    // once (StrictMode runs this effect twice).
    if (!reconciledRef.current) {
      reconciledRef.current = true;
      const state = history.state as { __reaperBack?: boolean } | null;
      if (state?.__reaperBack) {
        if (history.length > 1) {
          // Step back over the stale sentinel, consuming it, so the first real Back press does
          // something instead of dead-popping to the identical URL beneath it (B-12).
          //
          // Exactly ONE step, never a loop over however many entries a reload left stacked
          // (layers park one each). Stepping back off the reloaded entry crosses a document
          // boundary and loads the page again, which re-runs this reconcile -- so a loop here
          // is a loop over page loads, with its own bound reset by each one, walking the tab
          // out of the app. The leftovers past the first are harmless by comparison: they pop
          // to the same URL, costing a press each on the way out.
          selfPopRef.current += 1;
          history.back();
        } else {
          // Nothing to step back to: just clear the stale marker in place.
          history.replaceState(null, "");
        }
      }
    }
    return () => {
      window.removeEventListener("popstate", onPop);
      if (priorScrollRestoration !== null) history.scrollRestoration = priorScrollRestoration;
    };
  }, []);

  return (
    <BackNavContext.Provider value={api}>
      <ModalDepthContext.Provider value={modalDepth}>{children}</ModalDepthContext.Provider>
    </BackNavContext.Provider>
  );
}

/** Count this component's lifetime as one open modal. Called by ModalShell, which is the one
 *  modal in the app, so nothing else needs to call it. */
export function useModalLayer(): void {
  const ctx = useContext(BackNavContext);
  useEffect(() => ctx?.enterModal(), [ctx]);
}

/** Whether a modal is up. Read by the keyboard handlers that let ↑/↓/j/k walk a list: while a
 *  modal owns the keyboard, the list behind it must not move underneath.
 *
 *  This replaced `document.querySelector('[role="dialog"]')` in three of them -- a live DOM
 *  probe, run on every keypress, standing in for state React already owned. It answered by
 *  markup rather than by intent, so any future overlay that was modal without that attribute
 *  (or carried it without being modal, like a popover) would silently gain or lose the
 *  keyboard (H-2). */
export function useModalOpen(): boolean {
  return useContext(ModalDepthContext) > 0;
}

/** Record tab / section changes so Back restores the previous location. Returns `pushNav`. */
export function useBackNav(): { pushNav: (undo: () => void) => void } {
  const ctx = useContext(BackNavContext);
  // A no-op fallback keeps callers working if the provider is ever absent (tests, isolation).
  return { pushNav: ctx ? ctx.pushNav : () => {} };
}

/**
 * Register an overlay (menu, modal, side panel) with the Back button while it is `open`.
 * `close` is called if the user presses Back while this is the topmost open layer; closing it
 * any other way (Escape, an X, an outside click) auto-removes it. `close` is read fresh on each
 * Back, so a changing closure is fine.
 *
 * `canClose`, if given, is the same guard the modal's scrim / Escape / ✕ consult: while it
 * returns false a Back press is refused and the sentinel re-parked, so Back can never tear down
 * a modal that declared itself un-closable (a save in flight, say) -- rule 80.
 */
export function useBackGuard(
  open: boolean,
  close: () => void,
  canClose: () => boolean = () => true,
): void {
  const ctx = useContext(BackNavContext);
  const closeRef = useRef(close);
  closeRef.current = close;
  const canCloseRef = useRef(canClose);
  canCloseRef.current = canClose;
  // A LAYOUT effect, so the entry is parked before the browser paints the overlay. The picture a
  // browser files against the entry beneath is taken around the navigation that leaves it, and on
  // iOS that picture is what an edge-swipe back paints; parking first gives it the page the
  // reviewer is actually returning to rather than the overlay about to cover it. Driven in a real
  // browser at phone width to confirm the earlier timing still holds: the phone sheet's scroll
  // lock (App.tsx's usePageScrollLock, a passive effect) still captures the reviewer's offset,
  // and a Back still lands them on it.
  useLayoutEffect(() => {
    if (!ctx || !open) return;
    // `id` tracks the live registration. A refused Back -- the modal says it can't close yet,
    // the same guard the scrim/Escape/✕ honor -- re-registers, which re-parks the sentinel, so
    // Back stays armed instead of being spent on a close that never happened (rule 80). The
    // cleanup removes whichever id is current.
    let id = ctx.register(function onBack() {
      if (canCloseRef.current()) closeRef.current();
      else id = ctx.register(onBack);
    });
    return () => ctx.remove(id);
  }, [ctx, open]);
}
