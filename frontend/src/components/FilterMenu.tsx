// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The one anchored menu: a list of choices dropped under the control that opened it. The
// review queue's filter and value pickers and the list modal's IMDb presets all render this,
// so a menu opened anywhere in the app is the same menu (rule 18).
//
// The menu is absolutely positioned inside its anchor, which is what keeps it glued to its
// control as the page scrolls. Left-aligned to that anchor it ran clean off the right edge of
// a phone screen -- the ＋ Filter button sits at the end of the toolbar row, and a wrapped chip
// can land there too -- so `usePopoverShift` slides it back (see `popoverFit.ts`, rule 138).

import { useRef, type CSSProperties, type ReactNode } from "react";

import { usePopoverShift } from "./popoverFit";

export function FilterMenu({
  id,
  label,
  anchorClass = "filter-anchor",
  children,
}: {
  /** Pointed at by its trigger's `aria-controls`, which is the only thing tying the two
   *  together: the popover is a sibling of the button, not a descendant. */
  id: string;
  /** Names the menu for a screen reader, and heads it for everyone else. */
  label: string;
  /** The class of the positioned ancestor the menu hangs from and is measured against.
   *  `.filter-anchor` is the queue's inline wrapper; the list modal anchors on its own
   *  card, whose layout an inline wrapper would break. */
  anchorClass?: string;
  children: ReactNode;
}) {
  const ref = useRef<HTMLUListElement>(null);
  const shift = usePopoverShift(ref, anchorClass);

  // A plain list behind a disclosure, deliberately: this took `role="menu"` and `role="listbox"`
  // from a prop, and implemented neither one's keyboard contract -- no arrow keys, no roving
  // focus, no `aria-activedescendant`, and every option a separate Tab stop, which is not the
  // listbox pattern at all. A listbox is ANNOUNCED as an arrow-key widget, so the roles were
  // telling an operator to press keys that did nothing. App's UserMenu records the same defect
  // being fixed the same way, and PolicyRuleEditors' combobox shows what keeping the role costs.
  return (
    <ul
      ref={ref}
      id={id}
      className="filter-menu"
      aria-label={label}
      style={{ "--pop-shift": `${shift}px` } as CSSProperties}
    >
      <li className="filter-menu-head" aria-hidden="true">
        {label}
      </li>
      {children}
    </ul>
  );
}
