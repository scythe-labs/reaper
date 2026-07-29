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
