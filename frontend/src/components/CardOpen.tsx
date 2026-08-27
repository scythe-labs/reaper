// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The control that opens a card, and the card's accessible name, written once because four
// cards need it: the review queue's card, the show card's head, each season list item, and the
// Scales person card.
//
// The card itself is a plain container (`<article>`, `<li>`, `<div>`). Activation lives here, on
// a real `<button>` wrapping the title, the same pattern `SeasonExpander` and `SeasonStrip` use
// a few lines below. The whole-card click still opens it too, as a redundant mouse affordance.
//
// A card cannot be its own `role="button"` container. ARIA gives `role="button"` Children
// Presentational: True, which prunes every descendant from the accessibility tree and replaces
// it with that one label. A screen reader would hear only "Why {title} scored {n}, button" and
// miss the override chip, the held-reap note, the library, size, resolution, requester, reason
// line, and season strip beneath it, which is the evidence the queue exists to show. A
// `role="button"` container also cannot hold real `<button>`s inside it (Spare, Reap, the
// season expander, every strip square) without axe reporting `nested-interactive`, and on an
// `<li>` it removes the `listitem` role, so the list announces no item count.
//
// `role="group"` with `tabIndex={0}` on the container does not fix this either: it is a
// focusable element with no role a screen reader can announce, and it still nests interactive
// controls inside it.

import type { ReactNode } from "react";

export function CardOpen({
  name,
  pressed,
  onActivate,
  pressHandledByCard = false,
  children,
}: {
  /** What the control is called out loud. The card's title renders INSIDE it (`children`), and
   *  this name must contain that visible text, per WCAG 2.5.3 (Label in Name). For example,
   *  "Why {title} scored {n}" contains "{title}". The title row can show more than the name
   *  repeats, such as the year or a chip. That extra text sits outside this control, beside it
   *  in the heading, where it reads as ordinary card content instead of part of the button's
   *  name. */
  name: string;
  /** Whether this card is picked, used only in Select mode. Left undefined elsewhere, so the
   *  control renders as a plain button rather than a toggle claiming an off state it never
   *  leaves. */
  pressed?: boolean | undefined;
  onActivate: () => void;
  /** True when the card's own `onPointerDown` already acts on a press. That happens in Select
   *  mode, where the press both picks the card and arms the drag that paints a run of them. A
   *  click here would then toggle the same card back off, so the click stands down and only the
   *  keyboard path acts. Never set this where the card only has an `onClick`. `onOpen` is
   *  idempotent, so the redundant call costs nothing, and stopping it would be one more thing to
   *  keep in sync. */
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
        // The card behind this is a plain container, but it still opens on a click, and a
        // second `onOpen` call for the same id would be harmless. This stops the event anyway,
        // so one press stays one action even if a future card handler is not idempotent.
        e.stopPropagation();
        if (!pressHandledByCard) onActivate();
      }}
      onKeyDown={(e) => {
        if (e.key !== "Enter" && e.key !== " ") return;
        // `preventDefault` cancels the button's own activation, so the click this keypress would
        // otherwise create never fires, and `onActivate` runs exactly once. That holds in Select
        // mode too, where the click handler above is disabled. This also stops Space from
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
