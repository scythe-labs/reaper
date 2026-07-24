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
  // A parked sentinel entry survives a page reload (its pushState state persists), but parkedRef
  // resets to false on the fresh mount -- so on reload we would be sitting on a stale sentinel
  // with no layer behind it, and the first Back would pop it as a dead press. Reconciled once at
  // mount (below); StrictMode double-invokes effects, so this guards it to a single run.
  const reconciledRef = useRef(false);

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
          selfPopRef.current = true;
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
  useEffect(() => {
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
