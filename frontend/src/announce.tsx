// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The region the app says its successes out loud in.
//
// `Notice` solves failure: it owns `role="alert"` and mounts the moment it has something to
// say. Success needs a different mechanism, because Reaper signals success by something
// disappearing instead: the savebar unmounts, the modal closes, the composer's boxes empty,
// the row moves elsewhere on the page. An operator using a screen reader cannot perceive an
// absence. Pressing the policy page's primary action could produce nothing: no message, then
// no message, then a lost focus point. A save and a no-op press sounded identical, the wrong
// thing to leave unclear on the page that decides what gets deleted.
//
// **The region exists before its messages.** A polite region inserted into the DOM in the
// same commit as its own text is announced unreliably: several screen readers only watch
// regions that were already there, which is why `Notice` reaches for `role="alert"` instead.
// Mounting one region once, above every route, is what makes `polite` reliable. Polite, not
// assertive, since a save that worked must never cut off whatever the operator is reading.
//
// `useSlowWait` below applies the same fix to the app's loading affordances, with one added
// rule: a wait is only worth announcing once it has actually run long.
//
// **Why two regions.** Saying the same sentence twice counts as two announcements, but a text
// node whose content does not change is not announced again, so a second identical save would
// be silent. The message alternates between two always-mounted regions instead: whichever one
// receives it has changed, so it speaks, and the one it left goes empty with nothing left to
// read. A nonce plays the same role one layer up, in `SwitchConfirm`, for a repeat press.
//
// **Why a queue.** The store holds one sentence at a time, so a second `announce()` landing
// before the first has been read blanks the region holding it, and on the most common desktop
// stack that produces no accessibility event at all for the first sentence. This is not a
// timing race: one press of the policy page's Save fires two mutations whose `onSuccess`
// callbacks both announce, and both writes reliably land inside one accessibility-tree
// update. Screen readers never read the DOM directly; they read events derived from a
// batched, diffed accessibility tree, so a region going empty, then text, then empty again
// inside one batch is a net-zero diff and emits nothing. The second sentence still announces,
// matching the reported symptom exactly: one press, one sentence heard. So sentences are held
// in a queue and drained one `MESSAGE_GAP_MS` apart instead of overwriting each other.
//
// The deletion path uses this more than any single press does: it speaks at each stage of a
// run whose status polls every second, alongside every other surface in the app.

import { useEffect, useSyncExternalStore } from "react";

type Spoken = {
  text: string;
  /** Which of the two regions is holding `text` right now. */
  slot: 0 | 1;
};

const SILENT: Spoken = { text: "", slot: 0 };

/** How long a sentence keeps its region before the next one may replace it.
 *
 *  Not a guess at how long the sentence takes to say out loud, since that is unknowable. A
 *  screen reader queues a polite utterance itself once it notices the change, so a message it
 *  already picked up is not lost if the DOM moves on afterward. What this number has to cover
 *  is the window before that observation happens at all.
 *
 *  Chrome's rendering engine, Blink, only updates its accessibility tree every 150 ms after
 *  page load (`kDelayForDeferredUpdatesAfterPageLoad`), and it emits no live-region event of
 *  its own: the browser generates one by diffing two successive accessibility trees, so a
 *  region written and then blanked within that same window cancels out and announces nothing.
 *  That is what happened here: the policy page's Save can dirty both halves of the page at
 *  once, firing two announcements close enough together to land in the same window and lose
 *  the first one (`docs/LEARNINGS.md` has the measurement).
 *
 *  Safari's engine, WebKit, does not have this problem: its live-region timer flushes every
 *  task separately, so it never coalesces two announcements the way Blink does. Sizing for
 *  Blink's slower timer covers both browsers.
 *
 *  400 ms clears Blink's 150 ms window with a safe margin, while staying short enough that a
 *  run announcing several stages in a row still keeps up with itself. */
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
    // Nothing is mounted to hear it, so nothing can be said. This resets the message along
    // with the queue and its timer. Without the reset, a sentence held back by the gap would
    // surface at the next mount instead: an operator signing back in would hear "Policy
    // saved." from before they logged out, and in the test suite, one test's message would
    // read as the next test's.
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
 *  Call this where the operation settled, in a mutation's `onSuccess`, never its `onMutate`.
 *  Announcing at issuance would say a save worked while it is still in flight, and the
 *  failure that follows would arrive as a second, contradicting sentence.
 *
 *  The first sentence reaches the region in this same tick, so a caller speaking alone sees no
 *  change. Only a sentence arriving on top of one still holding its turn has to wait.
 *
 *  A plain function rather than a hook, because nearly every call site is a React Query
 *  `onSuccess` callback rather than a render. */
export function announce(text: string): void {
  if (text === "") return;
  // Nothing is mounted to hear it, so nothing can be said. This is the other end of the same
  // principle the unsubscribe above applies: without this guard, a sentence announced while no
  // region exists would sit in the store and surface at the next mount, the logged-out
  // operator hearing "Policy saved." on the way back in. `Announcer` is a sibling of every
  // branch at the app root (`App.tsx`, pinned by its own tests), so no shipped call site runs
  // before it; this guard actually matters for a component driven in isolation, where it
  // otherwise reads as one test's message showing up in the next test.
  if (listeners.size === 0) return;
  pending.push(text);
  if (drainTimer === null) drain();
}

/** How long a wait runs before it is worth announcing out loud.
 *
 *  This is not a measurement of a real reader's patience. It marks the point where silence
 *  stops reading as "the press has not landed yet" and starts reading as "the press did
 *  nothing". A lazily-loaded route lands well under this on any ordinary connection, so the
 *  common case stays quiet. Long enough that a fast page never speaks, short enough that a
 *  stalled one does not leave the operator wondering if the app is dead. */
const SLOW_WAIT_MS = 2000;

/** Say a wait out loud once it has run long enough to be worth interrupting for.
 *
 *  A loading affordance is markup: the spinner or skeleton stays purely visual, and a wait
 *  only speaks once it turns out to be slow. Announcing as soon as a wait starts would be the
 *  opposite mistake: a spinner that speaks on every route change is noise, since most loads
 *  finish inside a few hundred milliseconds. So the affordance keeps the picture, this keeps
 *  the sentence, and the sentence is only said when there was really something to wait for.
 *
 *  Pass the sentence while the wait is on and `null` when it is not, so the call sits above
 *  every branch of a component that renders its loading state conditionally. Unmounting, or
 *  passing `null` before the wait is up, cancels it, which is what keeps a fast load silent.
 *
 *  This is `announce`'s counterpart on the other side of timing: that function keeps a
 *  success from being announced before it has actually happened; this keeps a wait from being
 *  announced before it has actually run long. */
export function useSlowWait(sentence: string | null): void {
  useEffect(() => {
    if (sentence === null) return;
    const timer = setTimeout(() => announce(sentence), SLOW_WAIT_MS);
    return () => clearTimeout(timer);
  }, [sentence]);
}

/** The two live regions themselves. Mounted once, at the app root, above every branch, so it
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
