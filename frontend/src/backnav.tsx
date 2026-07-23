// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Make the browser Back button step *back through the UI* instead of leaving Reaper.
//
// The app has no router: where you are (which tab, which open panel, which menu) is plain
// React state, so a Back press used to navigate away from the whole page. This controller maps
// "somewhere to go back to" onto browser history -- closing the topmost open thing first, and
// only leaving the app once nothing is left open.
//
// The model is a SINGLE history sentinel, not one entry per layer. While anything is open we
// keep exactly one extra history entry parked. A Back press pops it; we close the topmost layer
// and, if more remain, park a fresh sentinel and wait for the next Back. So N open layers cost
// one browser entry and take N Back presses to unwind -- the browser never sees our internal
// depth, which keeps this robust however the layers nest.
//
// Two kinds of "layer", unwound newest-first:
//   - overlays (menus, modals, side panels): registered via `useBackGuard(open, close)` while
//     open, and auto-removed when they close by ANY means (Escape, the X, an outside click).
//   - navigation frames (a tab / section change): recorded via `pushNav(undo)`; a Back runs the
//     undo to restore the previous location. These persist until a Back consumes them.

import { createContext, useContext, useEffect, useRef, type ReactNode } from "react";

/** One thing a Back press can unwind. `onBack` closes an overlay or restores a prior tab. */
type Layer = { id: number; onBack: () => void };

type BackNavApi = {
  /** Register an open overlay; returns its id. Call `remove(id)` when it closes. */
  register: (onBack: () => void) => number;
  remove: (id: number) => void;
  /** Record a tab / section change so Back restores the previous one. */
  pushNav: (undo: () => void) => void;
};

const BackNavContext = createContext<BackNavApi | null>(null);

// The history entry we park. Marked so a stray popstate from elsewhere is still safe to handle.
const SENTINEL = { __reaperBack: true } as const;

export function BackNavProvider({ children }: { children: ReactNode }) {
  // A ref, not state: this controller renders nothing, and the popstate handler needs the live
  // list synchronously. Newest layer last.
  const layersRef = useRef<Layer[]>([]);
  const seqRef = useRef(0);
  // Whether our one sentinel entry is currently parked on the history stack.
  const parkedRef = useRef(false);
  // Set just before WE call history.back() ourselves (to un-park the sentinel when the last
  // layer closes by non-Back means), so the resulting popstate is ignored rather than treated
  // as a user Back.
  const selfPopRef = useRef(false);

  // Built once (lazily), not per render: every method touches only refs and globals, so a single
  // stable instance is correct and keeps the context value from changing.
  const apiRef = useRef<BackNavApi | null>(null);
  if (apiRef.current === null) {
    const park = () => {
      if (parkedRef.current) return;
      history.pushState(SENTINEL, "");
      parkedRef.current = true;
    };
    // Remove our parked sentinel by walking history back one step. The popstate it triggers is
    // swallowed via selfPopRef.
    const unpark = () => {
      if (!parkedRef.current) return;
      parkedRef.current = false;
      selfPopRef.current = true;
      history.back();
    };
    apiRef.current = {
      register(onBack) {
        const id = ++seqRef.current;
        layersRef.current = [...layersRef.current, { id, onBack }];
        park(); // idempotent: only the first open layer actually parks the sentinel
        return id;
      },
      remove(id) {
        const before = layersRef.current.length;
        layersRef.current = layersRef.current.filter((l) => l.id !== id);
        // Only the layer's own non-Back close reaches here for a still-present id. A Back press
        // slices the layer off first (below), so this is a no-op then -- no double-unpark.
        if (layersRef.current.length === before) return;
        if (layersRef.current.length === 0) unpark();
      },
      pushNav(undo) {
        const id = ++seqRef.current;
        layersRef.current = [...layersRef.current, { id, onBack: undo }];
        park();
      },
    };
  }
  const api = apiRef.current;

  useEffect(() => {
    const onPop = () => {
      if (selfPopRef.current) {
        // Our own unpark(); the sentinel is gone, nothing to unwind.
        selfPopRef.current = false;
        return;
      }
      const layers = layersRef.current;
      const top = layers[layers.length - 1];
      if (!top) {
        // Nothing open: the browser popped past our (absent) sentinel and is leaving. Let it.
        parkedRef.current = false;
        return;
      }
      // The browser just consumed our parked sentinel. Take the top layer off and, if more
      // remain, park a fresh sentinel so the next Back keeps unwinding rather than leaving.
      layersRef.current = layers.slice(0, -1);
      if (layersRef.current.length > 0) {
        history.pushState(SENTINEL, ""); // re-park; parkedRef stays true
      } else {
        parkedRef.current = false;
      }
      top.onBack();
    };
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  return <BackNavContext.Provider value={api}>{children}</BackNavContext.Provider>;
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
 */
export function useBackGuard(open: boolean, close: () => void): void {
  const ctx = useContext(BackNavContext);
  const closeRef = useRef(close);
  closeRef.current = close;
  useEffect(() => {
    if (!ctx || !open) return;
    const id = ctx.register(() => closeRef.current());
    return () => ctx.remove(id);
  }, [ctx, open]);
}
