// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The control that opens a card, and the card's accessible name -- written once because four
// cards need it and the fourth is how the bug below survived a review pass (rule 18/72).
//
// Every card in the review queue used to BE its own control: `<article role="button"
// aria-label="Why {title} scored {n}">`, plus the same shape on the show card's head, on each
// `<li>` of the season list, and on the Scales person card. ARIA gives `role="button"`
// **Children Presentational: True**, so every descendant is pruned from the accessibility tree
// and replaced by that one label. A screen reader heard "Why Example Title scored 62, button"
// and nothing else -- not the override chip saying a spare is keeping the file, not the
// held-reap "kept for now", not the library, size, resolution or requester, not the reason
// line, not the season strip. That pruned content IS the case for deleting the file, and it is
// the whole reason the queue exists (#169).
//
// Two more things came off the same declaration. Real `<button>`s sat inside the `role="button"`
// (Spare, Reap, the season expander, every strip square), which is invalid and which axe reports
// as `nested-interactive`; and the season list's `<li role="button">` lost `listitem`, so the
// list announced no item count.
//
// So the card is a plain container again -- `<article>`, `<li>`, `<div>` -- and the activation
// lives here, on a real control wrapping the title. That is the pattern `SeasonExpander` and
// `SeasonStrip` already used a few lines away in the same file. The whole-card click stays where
// it was, as a redundant mouse affordance: nothing about pointing at a card changes.
//
// Rejected: `role="group"` on the container with `tabIndex={0}` left in place. That is a
// focusable element with no role contract -- a Tab stop a screen reader has no word for -- and
// it keeps the nested-interactive violation.

import type { ReactNode } from "react";

export function CardOpen({
  name,
  pressed,
  onActivate,
  pressHandledByCard = false,
  children,
}: {
  /** What the control is called out loud. The card's title is rendered INSIDE (`children`), and
   *  this name has to contain that visible text, which is WCAG 2.5.3 (Label in Name): "Why
   *  {title} scored {n}" contains "{title}". Anything the title row shows that the name does not
   *  repeat -- the year, a chip -- therefore stays outside this control, beside it in the
   *  heading, where it is read as ordinary card content rather than as part of a button's name. */
  name: string;
  /** Select mode only: whether this card is picked. Left undefined elsewhere, so the control is
   *  a plain button rather than a toggle claiming an off state it never leaves. */
  pressed?: boolean | undefined;
  onActivate: () => void;
  /** True where the CARD's own `onPointerDown` already acts on a press -- Select mode, where the
   *  press both picks the card and arms the drag that paints a run of them. Then a click here
   *  would toggle the same card straight back, so the click stands down and only the keyboard
   *  path acts. Never set it where the card merely has an `onClick`: `onOpen` is idempotent, so
   *  the redundant call is free, and stopping it would be one more thing to keep in step. */
  pressHandledByCard?: boolean;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      className="card-open"
      aria-label={name}
      aria-pressed={pressed}
      onClick={(e) => {
        // The card behind this is a plain container now, but it still opens on a click, and a
        // second `onOpen` for the same id would be harmless. Stopped anyway so that one press
        // is one action wherever a future card handler is not idempotent.
        e.stopPropagation();
        if (!pressHandledByCard) onActivate();
      }}
      onKeyDown={(e) => {
        if (e.key !== "Enter" && e.key !== " ") return;
        // `preventDefault` cancels the button's own activation, so the click this keypress would
        // otherwise synthesize never fires and `onActivate` runs exactly once -- including in
        // Select mode, where the click path above is standing down. It also stops Space from
        // scrolling the queue out from under the operator.
        e.preventDefault();
        e.stopPropagation();
        onActivate();
      }}
    >
      {children}
    </button>
  );
}
