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
// Entries are interchangeable, not owned. A layer that closes gives an entry back off the top of
// the stack -- not the one it pushed, if it was not the newest -- and a layer that opens in the
// same tick takes over an entry already on its way back instead of pushing a second one. What is
// preserved is the COUNT, one per layer, which is what makes the newest entry's picture the page
// the newest layer covers.
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

// The history entry we park. Marked so a stray popstate from elsewhere is still safe to handle,
// and so we can ask the browser whether the entry we are on is one of ours before stepping off it.
const SENTINEL = { __reaperBack: true } as const;

// A backstop on the walk at mount over sentinels a reload left behind. It stops on its own at the
// first entry we did not park; this only bounds a stack we never built.
const RECONCILE_MAX_STEPS = 20;

export function BackNavProvider({ children }: { children: ReactNode }) {
  // A ref, not state: this controller renders nothing, and the popstate handler needs the live
  // list synchronously. Newest layer last.
  const layersRef = useRef<Layer[]>([]);
  const seqRef = useRef(0);
  // Entries we owe the browser back -- one per layer that closed by something other than a Back
  // press, counted only while the step is still unissued. Paid one settled step at a time, and
  // never inline: see `unpark`.
  const owedRef = useRef(0);
  const flushScheduledRef = useRef(false);
  // How many popstates WE caused (a step back to give an entry up) and must swallow rather than
  // treat as a user Back. A count, not a flag: two layers can close in one tick, and a flag would
  // let the second popstate through as a real Back.
  const selfPopRef = useRef(0);
  // Sentinel entries survive a page reload (their pushState state persists) while our counters
  // reset, so on reload we sit on stale sentinels with no layers behind them and each one is a
  // dead Back press. Walked off at mount (below); StrictMode double-invokes effects, so
  // `reconciledRef` guards that to a single run, and `reconcilingRef` tracks the walk in flight.
  const reconciledRef = useRef(false);
  const reconcilingRef = useRef(false);
  const reconcileStepsRef = useRef(0);

  // State, not a ref, because things RENDER off it (see useModalOpen). It costs no re-render of
  // the app: `children` arrives as an already-built element, so React skips that subtree when
  // this component re-renders, and only the hook's consumers update.
  const [modalDepth, setModalDepth] = useState(0);

  // Built once (lazily), not per render: every method touches only refs and globals, so a single
  // stable instance is correct and keeps the context value from changing.
  const apiRef = useRef<BackNavApi | null>(null);
  const stepRef = useRef<{ payOne: () => void; reconcile: () => void } | null>(null);
  if (apiRef.current === null) {
    // Whether the entry we are sitting on is one WE parked. The browser is the authority here,
    // never our own count: a long-press on Back jumps several entries and reports a single
    // popstate, and a reload leaves the two disagreeing outright. Stepping off an entry we did
    // not park walks the operator out of Reaper with a panel still open, so every step below asks
    // this first and does nothing rather than guess -- the same marker the walk at mount reads.
    const onOwnEntry = () =>
      (history.state as { __reaperBack?: boolean } | null)?.__reaperBack === true;

    // One step back, if the entry we are on is ours to give up. The popstate it triggers is
    // swallowed (selfPopRef) and drives the NEXT step, so every step reads `history.state` after
    // the previous one has landed rather than off a value the browser has not updated yet.
    const stepBack = () => {
      if (!onOwnEntry() || history.length <= 1) return false;
      selfPopRef.current += 1;
      history.back();
      return true;
    };

    // Pay one step of what we owe. If the browser says we are not on one of our entries, our
    // count had drifted ahead of the stack: drop the debt rather than navigate.
    const payOne = () => {
      if (owedRef.current === 0) return;
      owedRef.current = stepBack() ? owedRef.current - 1 : 0;
    };

    // Walk off the sentinels a reload left parked, one settled step at a time, stopping at the
    // first entry we did not park (the app's own first entry, whose state is null) or as soon as
    // a layer opens and claims the entry we are standing on. Measured in a real browser: these
    // are SAME-document traversals -- a popstate, no page load -- so walking all of them is a
    // handful of popstates, while stopping after one leaves every sentinel past the first as a
    // dead Back press (B-12). Bounded anyway, so no history stack can turn this into a spin.
    const reconcileStep = () => {
      if (layersRef.current.length > 0 || reconcileStepsRef.current >= RECONCILE_MAX_STEPS) {
        reconcilingRef.current = false;
        return;
      }
      if (onOwnEntry() && history.length <= 1) {
        // Nothing to step back to: clear the stale marker in place.
        history.replaceState(null, "");
        reconcilingRef.current = false;
        return;
      }
      reconcileStepsRef.current += 1;
      // Marked as in flight BEFORE the step, never from its result: the popstate that carries the
      // walk to its next step can arrive during `stepBack` itself, and would find this false.
      reconcilingRef.current = true;
      if (!stepBack()) reconcilingRef.current = false;
    };
    stepRef.current = { payOne, reconcile: reconcileStep };

    // One entry per layer, pushed as the layer opens -- which is also when the browser takes the
    // snapshot it will paint during an edge-swipe back (see the note at the top of this file).
    const park = () => {
      if (owedRef.current > 0 && onOwnEntry()) {
        // A layer closed earlier in this same tick and its entry has not gone back yet. Take it
        // over instead of pushing a second one on top of a step already owed. Only while we are
        // demonstrably standing on that entry: if the count has drifted there is nothing to take
        // over, and this layer needs one of its own or Back would leave with it still open.
        owedRef.current -= 1;
        return;
      }
      history.pushState(SENTINEL, "");
    };
    // Give this layer's entry back -- at the END of the tick, never from here. React runs every
    // layout-effect cleanup before any setup, so a layer closing while another opens calls this
    // first and `park` second; and a history.back() issued BEFORE a pushState in one tick
    // resolves against the entry that was current when it was called, discarding the entry just
    // pushed and leaving us a step lower than we think. Measured in a real browser, in both
    // orders. Deferring lets the opening layer take the entry over instead of racing it, and
    // turns several closes in one tick into one settled step at a time.
    const unpark = () => {
      owedRef.current += 1;
      if (flushScheduledRef.current) return;
      flushScheduledRef.current = true;
      queueMicrotask(() => {
        flushScheduledRef.current = false;
        payOne();
      });
    };
    apiRef.current = {
      register(onBack) {
        const id = ++seqRef.current;
        layersRef.current = [...layersRef.current, { id, onBack }];
        park(); // an entry of its own, even if others are already parked
        return id;
      },
      remove(id) {
        const before = layersRef.current.length;
        layersRef.current = layersRef.current.filter((l) => l.id !== id);
        // Only the layer's own non-Back close reaches here for a still-present id. A Back press
        // slices the layer off first (below), so this is a no-op then -- no double-unpark.
        if (layersRef.current.length === before) return;
        // Give an entry back, whether or not others remain open: the count is one per layer.
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
        // Our own step; that entry is gone, nothing to unwind. Whatever we are working through
        // takes its next step from here, now that `history.state` reflects where we landed.
        selfPopRef.current -= 1;
        if (owedRef.current > 0) stepRef.current?.payOne();
        else if (reconcilingRef.current) stepRef.current?.reconcile();
        return;
      }
      const layers = layersRef.current;
      const top = layers[layers.length - 1];
      if (!top) {
        // Nothing open: the browser popped an entry with no layer behind it and is on its way
        // out of the app. Let it.
        return;
      }
      // The browser just consumed the top layer's entry. Take that layer off; the layers still
      // open keep their own entries, so the next Back keeps unwinding rather than leaving.
      layersRef.current = layers.slice(0, -1);
      top.onBack();
    };
    window.addEventListener("popstate", onPop);
    // Walk off the sentinels a reload left parked (see reconcileStep). Done after the listener is
    // attached so our own steps are swallowed by selfPopRef and each one drives the next, and
    // only once (StrictMode runs this effect twice).
    if (!reconciledRef.current) {
      reconciledRef.current = true;
      stepRef.current?.reconcile();
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
