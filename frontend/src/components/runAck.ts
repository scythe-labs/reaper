// SPDX-License-Identifier: AGPL-3.0-or-later
// "The operator saw this run's result": one fact behind two dismissals, the shell bar's
// Dismiss and the Reap page's Done. One store, so pressing either hides both, and
// localStorage, so a page refresh does not resurrect a result already acknowledged. The
// status poll keeps reporting the last run forever, which is what makes the acknowledgment
// worth persisting at all.
import { useSyncExternalStore } from "react";

/** Exported for tests, which clear it between cases. */
export const ACK_KEY = "reaper-acked-run";

const listeners = new Set<() => void>();

// Holds a value ONLY when localStorage refused the write (blocked storage, full quota): the
// dismissal then lasts until the next full page load instead of across them, which is the
// old behavior, never worse. Never written on the success path, so clearing the stored key
// really clears the ack.
let fallback: number | null = null;

function read(): number | null {
  let raw: string | null;
  try {
    // window.localStorage, never the bare global: Node exposes an experimental global of
    // the same name, so the bare name is the wrong object under the test runner.
    raw = window.localStorage.getItem(ACK_KEY);
  } catch {
    return fallback;
  }
  if (raw === null) return fallback;
  const id = Number(raw);
  return Number.isFinite(id) ? id : fallback;
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** The run id whose result the operator dismissed, on either surface. */
export function useAckedRun(): number | null {
  return useSyncExternalStore(subscribe, read);
}

/** Record that the operator saw run `id`'s result. Every surface reading the ack re-renders. */
export function ackRun(id: number): void {
  try {
    window.localStorage.setItem(ACK_KEY, String(id));
  } catch {
    fallback = id;
  }
  for (const listener of listeners) listener();
}
