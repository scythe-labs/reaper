// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The two-step confirm that stands between an unsaved draft and the section you just pressed.
//
// Settings and the policy editor each grew their own copy of this, identical down to the button
// labels, and both had the same defect: the press was refused and nothing said so. Focus stayed
// on the control that appeared not to work, its own state did not change -- `aria-current` stayed
// put on the panel that was not left, the segment kept `aria-pressed="false"` -- and the warning
// mounted somewhere below with two new buttons nobody was told about. Pressing again was a
// literal no-op, because `setPendingSwitch` with the value it already holds is not a state
// change in React. On a narrow screen it was worse: the picker is a controlled `<select>`, so it
// announced the section the operator chose and then silently snapped back.
//
// So this moves focus into the notice. That is the load-bearing half of the fix, ahead of the
// `role="alert"` that `Notice` now carries: it tells the operator the press did something, and
// it puts Discard and Keep editing one Tab away instead of past the whole section rail.
//
// Keyed on a NONCE, not on the pending value. A second press of the same section produces no
// state change at all, so an effect watching `pendingSwitch` would not re-fire and the second
// press would stay the no-op it always was. The caller bumps the nonce on every refused press.

import { useEffect, useRef } from "react";
import { Notice } from "./Notice";

export function SwitchConfirm({
  nonce,
  message,
  onDiscard,
  onKeep,
}: {
  /** Bumped by the caller on every refused press, including a repeat of the same one. */
  nonce: number;
  /** What switching would cost, in this surface's own words. */
  message: string;
  onDiscard: () => void;
  onKeep: () => void;
}) {
  const ref = useRef<HTMLElement>(null);

  // Declared BEFORE the focus move below, because effects run in declaration order and this one
  // has to read `activeElement` while it is still the control that was pressed.
  //
  // Taking focus is only half a loan. When the notice unmounts -- Keep editing, or the switch
  // going through -- the focused node goes with it and `activeElement` falls back to `<body>`,
  // so the next Tab starts at the masthead and the operator walks the user menu and the whole
  // section rail back to the field they were editing. That is the cost this component exists to
  // remove, paid on the way out instead of the way in. Guarded on `contains`, because Discard
  // can take the trigger with it, and focusing a detached node silently does nothing.
  useEffect(() => {
    const trigger = document.activeElement;
    return () => {
      if (trigger instanceof HTMLElement && document.contains(trigger)) trigger.focus();
    };
  }, []);

  useEffect(() => {
    ref.current?.focus();
  }, [nonce]);

  return (
    <Notice tone="warn" as="div" ref={ref} tabIndex={-1}>
      {message}{" "}
      <button type="button" className="danger" onClick={onDiscard}>
        Discard and switch
      </button>{" "}
      <button type="button" className="ghost" onClick={onKeep}>
        Keep editing
      </button>
    </Notice>
  );
}
