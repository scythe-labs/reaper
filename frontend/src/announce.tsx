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
// **Why two regions.** Saying the same sentence twice is two announcements, and a text node that
// does not change is not announced -- the second save would be as silent as the bug this
// replaces. So the message alternates between two always-mounted regions: whichever one receives
// it has changed, so it speaks. The one it left goes empty, and an empty atomic region has
// nothing to read out. A nonce is the same trick `SwitchConfirm` needed for a repeat press, one
// layer up.
//
// **Why a queue.** The store holds ONE sentence, so a second `announce()` landing before the
// first has had a turn blanks the region holding it -- and on the most common desktop stack the
// first sentence then produces no accessibility event at all. It is not a race and not a DOM
// problem: one press of the policy page's Save fires two mutations whose `onSuccess` callbacks
// both announce, and driven in a real browser the two writes land inside ONE serialization batch
// in 10 runs out of 10 (#193). Screen readers never read the DOM; they get events derived from a
// batched, diffed accessibility tree, so a region going "" -> text -> "" inside one batch is a
// net-zero diff and nothing is emitted for it. The second sentence still announces, which is
// exactly the reported symptom: one press, one sentence. So sentences are held and drained one
// `MESSAGE_GAP_MS` apart instead of overwriting.
//
// The deletion path makes it broader than that one press: it speaks at each stage of a run whose
// status polls every second, beside every other surface in the app.

import { useSyncExternalStore } from "react";

type Spoken = {
  text: string;
  /** Which of the two regions is holding `text` right now. */
  slot: 0 | 1;
};

const SILENT: Spoken = { text: "", slot: 0 };

/** How long a sentence keeps the region before the next one may replace it.
 *
 *  Not a guess at how long the sentence takes to SAY -- that is unknowable, and an AT queues a
 *  polite utterance itself once it has observed the change, so a message already picked up is
 *  not lost when the DOM moves on. What this clears is the window in which the observation is
 *  never made at all.
 *
 *  **The number that decides that is Blink's 150 ms, not a rendering frame** (`constexpr int
 *  kDelayForDeferredUpdatesAfterPageLoad = 150` -- `CommitAXUpdates` early-returns until it
 *  elapses, so a batch spans many frames). Blink emits no live-region event itself; the browser
 *  process generates them by diffing successive accessibility trees, so a write and its blanking
 *  inside one batch cancel out and nothing fires. Measured on the policy page's both-halves-dirty
 *  Save: both writes complete within 74 ms of the click, every run, so they shared one batch and
 *  the first sentence was reliably lost (#193, and `docs/LEARNINGS.md`).
 *
 *  WebKit is the opposite and the folklore about it is stale: its live-region timer is
 *  `startOneShot(0_s)`, so writes in separate tasks get separate flushes and are not coalesced
 *  away. Sizing for Blink covers both.
 *
 *  400 clears 150 nearly three times over -- measured at a 386-389 ms real gap against this
 *  implementation -- and is short enough that a run announcing its stages keeps up with itself. */
const MESSAGE_GAP_MS = 400;

let spoken: Spoken = SILENT;
/** Sentences said while an earlier one still holds the region, oldest first. */
let pending: string[] = [];
let drainTimer: ReturnType<typeof setTimeout> | null = null;
const listeners = new Set<() => void>();

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
    // Nothing is mounted to hear it, so nothing can be said. Reset -- the queue and its timer
    // along with the message, or a sentence held back by the gap would surface in the NEXT
    // mount: a logged-out operator signing back in to "Policy saved.", and, in the suite, one
    // test's message read as the next test's.
    if (listeners.size === 0) {
      spoken = SILENT;
      pending = [];
      if (drainTimer !== null) {
        clearTimeout(drainTimer);
        drainTimer = null;
      }
    }
  };
}

function emit(): void {
  for (const listener of listeners) listener();
}

/** Hand the region to the next sentence, or let it fall quiet. */
function drain(): void {
  drainTimer = null;
  const next = pending.shift();
  if (next === undefined) return;
  spoken = { text: next, slot: spoken.slot === 0 ? 1 : 0 };
  emit();
  drainTimer = setTimeout(drain, MESSAGE_GAP_MS);
}

/** Say something happened, in a whole sentence an operator would recognize as the thing they
 *  just did ("Policy saved.", not "saved").
 *
 *  Call this where the operation SETTLED -- a mutation's `onSuccess`, never its `onMutate`
 *  (rule 85). Announcing at issuance says a save worked while it is still in flight, and the
 *  failure that follows arrives as a second, contradicting sentence.
 *
 *  The first sentence reaches the region in this same tick, so a caller speaking alone is
 *  unchanged. Only one arriving on top of a sentence still holding its turn waits.
 *
 *  A plain function rather than a hook, because nearly every call site is a React Query
 *  `onSuccess` callback rather than a render. */
export function announce(text: string): void {
  if (text === "") return;
  pending.push(text);
  if (drainTimer === null) drain();
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
