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
// press would stay the no-op it always was. `useSwitchConfirm` below bumps the nonce on every
// refused press, so no caller has to know that.

import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
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
  const { t } = useTranslation();
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
        {t("shell.switchConfirm.discardAndSwitch")}
      </button>{" "}
      <button type="button" className="ghost" onClick={onKeep}>
        {t("shell.switchConfirm.keepEditing")}
      </button>
    </Notice>
  );
}

/** The caller half of the two-step confirm: refuse the switch while there are edits to lose,
 *  hold the destination, and clear itself once there is nothing left to warn about.
 *
 *  Settings and the policy editor each wrote this out. The policy editor found B-31 on its own,
 *  a notice keyed to the Discard handler surviving a Save and still offering "Discard and
 *  switch" for changes that no longer existed. Settings' copy then carried a comment pointing
 *  at that fix rather than sharing it.
 *
 *  Three things have to be right together here, and none of them is local to a caller: the
 *  nonce bumps on every refused press, the pending destination clears on `dirty` going false,
 *  and a press of the section already open is not a switch at all.
 *
 *  `commit` is what actually moves: `setPanel` in Settings, `setMediaType` in the policy editor.
 *  It is called only from `discard`, or from `request` when there was nothing to lose. */
export function useSwitchConfirm<T>(current: T, dirty: boolean, commit: (next: T) => void) {
  const [pending, setPending] = useState<T | null>(null);
  const [nonce, setNonce] = useState(0);

  // The notice exists only because there are edits to lose, so it goes when they do, by Discard
  // or by a Save that stores them. Keyed on the draft rather than on the Discard handler so the
  // save path is covered too. A Save clears the notice and switches nothing.
  useEffect(() => {
    if (!dirty) setPending(null);
  }, [dirty]);

  return {
    pending,
    nonce,
    /** Ask to switch. Goes straight through when there is nothing to lose. */
    request: (next: T) => {
      if (next === current) return;
      if (dirty) {
        setPending(next);
        setNonce((n) => n + 1);
        return;
      }
      setPending(null);
      commit(next);
    },
    discard: () => {
      setPending(null);
      if (pending !== null) commit(pending);
    },
    keep: () => setPending(null),
  };
}
