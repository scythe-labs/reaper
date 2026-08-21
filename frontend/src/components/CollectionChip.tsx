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

import { useId, useRef, type RefObject } from "react";
import { createPortal } from "react-dom";
import { useTranslation } from "react-i18next";
import { useDialogFocus } from "../focus";
import { trapTab } from "./ModalShell";
import { useFixedMenu, type MenuPos } from "./popoverFit";
import { CaretDownGlyph, CollectionIcon } from "./queueIcons";

export function CollectionChip({
  collections,
  matched = null,
  sizes = null,
  onOpen,
}: {
  /** This item's collection names, already sorted smallest-first by the scan. Null or empty
   *  renders nothing -- a failed Plex read must never degrade into an empty chip. */
  collections: string[] | null | undefined;
  /** The collection a collection-name search matched (`search_rank === 2`), if this row is one.
   *  When set, the chip's own name is THIS one rather than `collections[0]` -- everywhere else
   *  the smallest collection wins the name, but here that would put an unrelated one on a row
   *  the operator could not otherwise explain (#816 phase 3b's one exception to smallest-first;
   *  the backend end of this comment is `CandidateOut.matched_collection`). Still a member of
   *  `collections`, so the picker's full list is unaffected. */
  matched?: string | null;
  /** Each known collection's Plex member count, from the snapshot (`Snapshot.collection_sizes`).
   *  A collection this map omits has no KNOWN size (Plex never reported one), which is a
   *  different fact from a size of zero, so the picker renders no number for it rather than a
   *  false "0". */
  sizes?: Record<string, number> | null;
  /** Open the collection screen on the given collection name. Called by the chip's own name
   *  and by every row of its picker. */
  onOpen: (name: string) => void;
}) {
  const { t } = useTranslation();
  const popId = useId();
  const caretRef = useRef<HTMLButtonElement>(null);
  // Fixed, clamped to the viewport (rule 138, #816 phase 4 fence): `.card` sets `overflow:
  // hidden` for its backdrop art, so an absolutely positioned popover would be clipped to the
  // card and most of the list unreachable. HEIGHT is a rough upper bound for the flip decision
  // only, never a coordinate -- the portal below measures its own rendered height for nothing.
  const {
    pos: popAt,
    menuRef,
    toggle: toggleOpen,
    close: closePicker,
  } = useFixedMenu<HTMLUListElement>(caretRef, {
    width: 224,
    height: 40 + Math.min(collections?.length ?? 1, 8) * 30,
  });

  if (!collections || collections.length === 0) return null;
  const name = matched ?? collections[0];
  const rest = collections.length - 1;

  return (
    <span className="coll-chip">
      <button
        type="button"
        className={`coll-chip-main ${rest === 0 ? "coll-chip-only" : ""}`}
        title={t("scales.collectionChip.inCollection", { name })}
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
          aria-label={t("scales.collectionChip.showOthers", { rest })}
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
          sizes={sizes}
          menuRef={menuRef}
          onOpen={onOpen}
          onClose={closePicker}
        />
      )}
    </span>
  );
}

/** The rest of this item's collections, opened by the caret. Portaled to `<body>` and rendered
 *  `position: fixed` at `at`, same reason and same technique as `OverrideControls`' `SpareMenu`:
 *  the card clips its overflow AND stacks its own children, either of which a fixed child alone
 *  cannot escape.
 *
 *  Its open/close state, its clamped position, and the outside-click/Escape/scroll dismissal all
 *  live in `useFixedMenu` (`components/popoverFit.ts`), called by `CollectionChip` above --
 *  `menuRef` is that hook's, threaded down so this component only has to draw. */
function CollectionPicker({
  at,
  popId,
  names,
  sizes,
  menuRef,
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
  /** Each known collection's Plex member count. A name this map omits renders no number (its
   *  size was never reported), never a false "0" -- see `CollectionChip`'s own doc on `sizes`. */
  sizes: Record<string, number> | null;
  menuRef: RefObject<HTMLUListElement | null>;
  onOpen: (name: string) => void;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const firstItem = useRef<HTMLButtonElement>(null);
  useDialogFocus(menuRef, true, firstItem);

  return createPortal(
    <ul
      ref={menuRef}
      id={popId}
      className="coll-pop"
      aria-label={t("scales.collectionChip.pickerLabel")}
      tabIndex={-1}
      onKeyDown={(e) => trapTab(e, menuRef.current)}
      style={{
        position: "fixed",
        left: at.left,
        ...(at.top !== undefined ? { top: at.top } : { bottom: at.bottom }),
      }}
      onClick={(e) => e.stopPropagation()}
    >
      {names.map((name, i) => {
        const size = sizes?.[name];
        return (
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
              {size !== undefined && <span className="coll-pop-n">{size}</span>}
            </button>
          </li>
        );
      })}
    </ul>,
    document.body,
  );
}
