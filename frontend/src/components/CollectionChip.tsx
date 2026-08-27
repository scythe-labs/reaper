// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The chip that names an item's Plex collection. One component renders it on the movie card,
// the show card, and the why panel's facts line, so a picker fix lands everywhere at once. A
// collection view's own rows render the same card components, so that surface is covered for
// free too.
//
// Collections are for navigation, never for protection. This reads `collections` off the
// candidate purely to display it, never to gate, score, or decide anything. A null array (Plex
// not configured, a failed section read, or an old row from before this shipped) renders no
// chip rather than an empty one.
//
// One chip covers however many collections an item is in. The smallest one already sits at
// `collections[0]`, since the scan sorts smallest-first with ties broken alphabetically, so the
// chip's own name never needs the rest. More than one collection adds a caret that splits off
// the name, the same anatomy `OverrideControls`' Spare button uses for its length menu
// (`.ov-split` / `.split-main` / `.split-caret`), sized down for this quieter chip.
//
// The name and every picker row navigate: `onOpen` opens the collection screen on that name.
// Both stop propagation, for the same reason the caret does. The chip sits inside a card whose
// own click opens the why panel, and a click anywhere inside the card bubbles up unless it is
// stopped here.

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
  /** This item's collection names, sorted smallest-first by the scan. Null or empty renders
   *  nothing. A failed Plex read must never show as an empty chip. */
  collections: string[] | null | undefined;
  /** The collection a collection-name search matched (`search_rank === 2`), if this row is one.
   *  When set, the chip's own name is this one instead of `collections[0]`. Everywhere else the
   *  smallest collection wins the name, but here that would show an unrelated one on a row the
   *  operator could not otherwise explain. See the backend field
   *  `CandidateOut.matched_collection`. This value stays a member of `collections`, so the
   *  picker's full list is unaffected. */
  matched?: string | null;
  /** Each known collection's Plex member count, from the snapshot (`Snapshot.collection_sizes`).
   *  A collection this map omits has no KNOWN size, since Plex never reported one. That is a
   *  different fact from a size of zero, so the picker renders no number for it instead of a
   *  false "0". */
  sizes?: Record<string, number> | null;
  /** Open the collection screen on the given collection name. Called by the chip's own name
   *  and by every row of its picker. */
  onOpen: (name: string) => void;
}) {
  const { t } = useTranslation();
  const popId = useId();
  const caretRef = useRef<HTMLButtonElement>(null);
  // Fixed position, clamped to the viewport, since `.card` sets `overflow: hidden` for its
  // backdrop art. An absolutely positioned popover would be clipped to the card, making most of
  // the list unreachable. HEIGHT here is only a rough upper bound for the flip decision, never
  // an exact coordinate. The portal below measures its own rendered height when it positions
  // itself.
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
          // This is non-null: the guard above already ruled out an empty array, so element 0
          // exists. TypeScript cannot carry that fact through a plain array read.
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
 *  `position: fixed` at `at`, the same technique `OverrideControls`' `SpareMenu` uses. The card
 *  clips its overflow and stacks its own children, and a fixed child alone cannot escape either
 *  one.
 *
 *  Its open/close state, its clamped position, and the outside-click, Escape, and scroll
 *  dismissal all live in `useFixedMenu` (`components/popoverFit.ts`), called by `CollectionChip`
 *  above. `menuRef` comes from that hook, threaded down so this component only has to draw. */
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
  /** Every one of this item's collections, smallest first. This is the full list, including the
   *  one already shown as the chip's own name, so picking it again is one row instead of a dead
   *  end. */
  names: string[];
  /** Each known collection's Plex member count. A name this map omits renders no number, since
   *  its size was never reported, rather than a false "0". See `CollectionChip`'s own doc on
   *  `sizes`. */
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
        insetInlineStart: at.start,
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
