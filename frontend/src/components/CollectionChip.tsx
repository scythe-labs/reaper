// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The chip that names an item's Plex collection: one component, rendered on the movie card, the
// show card, and the why panel's facts line -- so a picker fix lands everywhere at once (rule
// 18). A collection view's own rows render the same card components, so that surface is covered
// for free once these three are (#816 phase 4).
//
// Collections are navigation, never protection (#816's fence): this reads `collections` off the
// candidate purely to display it, never to gate, score, or decide anything, and a null array
// (Plex not configured, a failed section read, a row from before this shipped) renders no chip
// rather than an empty one.
//
// One chip however many collections an item is in: the smallest one already sits at
// `collections[0]` (the scan sorts smallest-first, ties alphabetical), so the chip's own name
// never needs the rest. More than one collection adds a caret, split off the name the way
// `OverrideControls`' Spare button splits off its length menu (`.ov-split` / `.split-main` /
// `.split-caret`) -- the same anatomy, sized to the quiet chip family instead of a button.
//
// The name and every picker row navigate: `onOpen` opens the collection screen on that name
// (#816 phase 5). Both `stopPropagation`, the same reason the caret already does -- the chip
// sits inside a card whose own click opens the why-panel (rule 60's sibling: the card is a
// plain clickable element, not a keydown row, so a click anywhere inside it bubbles up unless
// stopped here).

import {
  useEffect,
  useId,
  useRef,
  useState,
  type MouseEvent as ReactMouseEvent,
  type RefObject,
} from "react";
import { createPortal } from "react-dom";
import { useBackGuard } from "../backnav";
import { useDialogFocus } from "../focus";
import { trapTab } from "./ModalShell";
import type { MenuPos } from "./OverrideControls";
import { CaretDownGlyph, CollectionIcon } from "./queueIcons";

export function CollectionChip({
  collections,
  onOpen,
}: {
  /** This item's collection names, already sorted smallest-first by the scan. Null or empty
   *  renders nothing -- a failed Plex read must never degrade into an empty chip. */
  collections: string[] | null | undefined;
  /** Open the collection screen on the given collection name. Called by the chip's own name
   *  and by every row of its picker. */
  onOpen: (name: string) => void;
}) {
  const popId = useId();
  const caretRef = useRef<HTMLButtonElement>(null);
  const [popAt, setPopAt] = useState<MenuPos | null>(null);
  useBackGuard(popAt !== null, () => setPopAt(null));

  if (!collections || collections.length === 0) return null;
  const [name] = collections;
  const rest = collections.length - 1;

  // Fixed, clamped to the viewport, the way `OverrideControls.toggleMenu` places the Spare
  // length menu: `.card` sets `overflow: hidden` for its backdrop art, so an absolutely
  // positioned popover would be clipped to the card and most of the list unreachable (rule 138,
  // #816 phase 4 fence). HEIGHT is a rough upper bound for the flip decision only, never a
  // coordinate -- the portal below measures its own rendered height for nothing.
  const toggleOpen = (e: ReactMouseEvent) => {
    e.stopPropagation();
    if (popAt) {
      setPopAt(null);
      return;
    }
    const btn = caretRef.current;
    if (!btn) return;
    const r = btn.getBoundingClientRect();
    const WIDTH = 224;
    const HEIGHT = 40 + Math.min(collections.length, 8) * 30;
    const left = Math.max(8, Math.min(r.right - WIDTH, window.innerWidth - WIDTH - 8));
    const below = r.bottom + HEIGHT <= window.innerHeight;
    setPopAt(
      below ? { left, top: r.bottom + 4 } : { left, bottom: window.innerHeight - r.top + 4 },
    );
  };

  return (
    <span className="coll-chip">
      <button
        type="button"
        className={`coll-chip-main ${rest === 0 ? "coll-chip-only" : ""}`}
        title={`In the collection "${name}"`}
        onClick={(e) => {
          e.stopPropagation();
          // Non-null: the guard above already refused an empty array, so element 0 exists --
          // TS just can't carry that through a plain array's destructure.
          onOpen(name!);
        }}
      >
        <CollectionIcon />
        {name}
      </button>
      {rest > 0 && (
        <button
          ref={caretRef}
          type="button"
          className="coll-chip-caret"
          aria-expanded={popAt !== null}
          aria-controls={popAt !== null ? popId : undefined}
          aria-label={`Show the other ${rest} collection${rest === 1 ? "" : "s"}`}
          onClick={toggleOpen}
        >
          <CaretDownGlyph />
        </button>
      )}
      {popAt && (
        <CollectionPicker
          at={popAt}
          popId={popId}
          names={collections}
          triggerRef={caretRef}
          onOpen={onOpen}
          onClose={() => setPopAt(null)}
        />
      )}
    </span>
  );
}

/** The rest of this item's collections, opened by the caret. Portaled to `<body>` and rendered
 *  `position: fixed` at `at`, same reason and same technique as `OverrideControls`' `SpareMenu`:
 *  the card clips its overflow AND stacks its own children, either of which a fixed child alone
 *  cannot escape. */
function CollectionPicker({
  at,
  popId,
  names,
  triggerRef,
  onOpen,
  onClose,
}: {
  at: MenuPos;
  /** Pointed at by the caret's `aria-controls`, which is the only thing tying the two together
   *  once the portal puts this panel at the end of `<body>`. */
  popId: string;
  /** Every one of this item's collections, smallest first -- the full list, including the one
   *  already shown as the chip's own name, so picking it back is one row rather than a dead
   *  end. */
  names: string[];
  triggerRef: RefObject<HTMLButtonElement | null>;
  onOpen: (name: string) => void;
  onClose: () => void;
}) {
  const ref = useRef<HTMLUListElement>(null);
  const firstItem = useRef<HTMLButtonElement>(null);
  useDialogFocus(ref, true, firstItem);

  const closeRef = useRef(onClose);
  closeRef.current = onClose;

  useEffect(() => {
    const close = () => closeRef.current();
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node;
      if (!ref.current?.contains(t) && !triggerRef.current?.contains(t)) close();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      // The newest layer consumes Escape, so it never also closes the why-panel this chip
      // sits inside (the same reason SpareMenu stops it, rule 72's sibling).
      e.stopPropagation();
      close();
    };
    const onScroll = () => close();
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    window.addEventListener("scroll", onScroll, true);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("scroll", onScroll, true);
    };
  }, [triggerRef]);

  return createPortal(
    <ul
      ref={ref}
      id={popId}
      className="coll-pop"
      aria-label="Collections"
      tabIndex={-1}
      onKeyDown={(e) => trapTab(e, ref.current)}
      style={{
        position: "fixed",
        left: at.left,
        ...(at.top !== undefined ? { top: at.top } : { bottom: at.bottom }),
      }}
      onClick={(e) => e.stopPropagation()}
    >
      {names.map((name, i) => (
        <li key={name}>
          <button
            type="button"
            className="coll-pop-item"
            ref={i === 0 ? firstItem : undefined}
            onClick={() => {
              onOpen(name);
              onClose();
            }}
          >
            <span className="coll-pop-name">{name}</span>
          </button>
        </li>
      ))}
    </ul>,
    document.body,
  );
}
