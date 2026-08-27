// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The one anchored menu: a list of choices dropped under the control that opened it. The
// review queue's filter and value pickers and the list modal's IMDb presets all render this,
// so a menu opened anywhere in the app is the same menu.
//
// The menu is absolutely positioned inside its anchor, which keeps it glued to its control as
// the page scrolls. Left-aligned to that anchor, it ran clean off the right edge of a phone
// screen. The ＋ Filter button sits at the end of the toolbar row, and a wrapped chip can land
// there too, so `usePopoverShift` slides it back (see `popoverFit.ts`).

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
   *  `.filter-anchor` is the queue's inline wrapper. The list modal anchors on its own
   *  card instead, since an inline wrapper would break its layout. */
  anchorClass?: string;
  children: ReactNode;
}) {
  const ref = useRef<HTMLUListElement>(null);
  const shift = usePopoverShift(ref, anchorClass);

  // This is deliberately a plain list behind a disclosure, not `role="menu"` or `role="listbox"`.
  // Neither role's keyboard contract is implemented here. There are no arrow keys, no roving
  // focus, no `aria-activedescendant`, and every option is its own Tab stop, which is not the
  // listbox pattern. A listbox is announced as an arrow-key widget, so those roles would tell
  // an operator to press keys that do nothing. App's `UserMenu` fixes the same defect the same
  // way, and `PolicyRuleEditors`' combobox shows what keeping the role costs.
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
