// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The region the app says its successes out loud in.
//
// Failure was solved by `Notice`, which owns `role="alert"` and is mounted at the moment it has
// something to say. Success has the opposite shape and could not reuse it: Reaper signals a
// success by something DISAPPEARING -- the savebar unmounts, the modal closes, the composer's
// boxes empty, the row appears somewhere else on the page -- and an operator using a screen
// reader cannot perceive an absence. Press the policy page's primary action and there was
// nothing: no message, then no message, then a lost focus point. Save and a no-op press were
// indistinguishable, which on the page that decides what gets deleted is the wrong thing to be
// unsure about.
//
// **The region pre-exists its messages, and that is the whole reason it is a module.** A polite
// region inserted into the DOM in the same commit as its text is unreliably announced -- several
// readers only watch regions that were already there -- which is exactly why `Notice` had to
// reach for `role="alert"` instead, and why the two bare `role="status"` toasts in `ReviewQueue`
// are silent today. Mounting one region once, above every route, is the shape that makes
// `polite` work. Polite and not assertive: a save that worked must not cut off whatever the
// operator is reading.
//
// **Why two regions.** Saving twice says "Policy saved." twice, and a text node that does not
// change is not announced -- the second save would be as silent as the bug this replaces. So the
// message alternates between two always-mounted regions: whichever one receives it has changed,
// so it speaks. The one it left goes empty, and an empty atomic region has nothing to read out.
// A nonce is the same trick `SwitchConfirm` needed for a repeat press, one layer up.

import { useSyncExternalStore } from "react";

type Spoken = {
  text: string;
  /** Which of the two regions is holding `text` right now. */
  slot: 0 | 1;
};

const SILENT: Spoken = { text: "", slot: 0 };

let spoken: Spoken = SILENT;
const listeners = new Set<() => void>();

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
    // Nothing is mounted to hear it, so nothing can be said. Reset, or the next mount opens
    // holding the last thing the previous one announced -- a logged-out operator signing back
    // in to "Policy saved.", and, in the suite, one test's message read as the next test's.
    if (listeners.size === 0) spoken = SILENT;
  };
}

/** Say something happened, in a whole sentence an operator would recognize as the thing they
 *  just did ("Policy saved.", not "saved").
 *
 *  Call this where the operation SETTLED -- a mutation's `onSuccess`, never its `onMutate`
 *  (rule 85). Announcing at issuance says a save worked while it is still in flight, and the
 *  failure that follows arrives as a second, contradicting sentence.
 *
 *  A plain function rather than a hook, because nearly every call site is a React Query
 *  `onSuccess` callback rather than a render. */
export function announce(text: string): void {
  if (text === "") return;
  spoken = { text, slot: spoken.slot === 0 ? 1 : 0 };
  for (const listener of listeners) listener();
}

/** The two live regions themselves. Mounted once, at the app root, above every branch -- so it
 *  outlives a route change, a Suspense fallback and a logout, and exists before the first thing
 *  that speaks into it. */
export function Announcer() {
  const now = useSyncExternalStore(
    subscribe,
    () => spoken,
    () => SILENT,
  );

  return (
    <>
      <div className="sr-only" role="status" aria-live="polite" aria-atomic="true">
        {now.slot === 0 ? now.text : ""}
      </div>
      <div className="sr-only" role="status" aria-live="polite" aria-atomic="true">
        {now.slot === 1 ? now.text : ""}
      </div>
    </>
  );
}
