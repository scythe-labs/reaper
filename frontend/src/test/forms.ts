// SPDX-License-Identifier: AGPL-3.0-or-later
// Puts a value in a text box the way a test means it: as ONE edit, not as N keystrokes.
//
// `userEvent.type(box, "https://reaper.example.com")` dispatches twenty-six keystrokes, and
// every one of them re-renders the panel around the box. Locally that reads as a rounding
// error. It is not one under load: the same file measured 13x slower on a busy CI runner, and
// the cost is per keystroke, so the SLOWEST test in a family is the one that typed the most.
// `SettingsNav`'s six General tests sat at 3.0-4.7s against vitest's 5000ms default, and the
// longest of them went over and failed the run -- a timeout with no bug under it, on a suite
// that was green on the same commit an hour earlier.
//
// What makes this the fix rather than a bigger timeout is the TAIL. Measured over five runs of
// that family, `type` had a median of 116ms and a worst case of 405ms; the same drafts filled
// through here measured 37ms median and 39ms worst. One event has almost no spread to have,
// so the number that decides whether a run goes red stops moving.
//
// Use it wherever the typing is SETUP -- where the test asserts on the state the form ends in.
// Keep `type` where the keystrokes are the point: a box that validates as you go, a button that
// enables partway through, anything appending to what is already in the box (this clears
// first), and any string carrying user-event's `{...}` key syntax, which a paste would deliver
// as literal text.
import { waitFor } from "@testing-library/react";
import type { UserEvent } from "@testing-library/user-event";
import { expect } from "vitest";

/**
 * Fill `box` with `value` in one edit, replacing whatever it held.
 *
 * Waits for the box to be enabled first, because user-event reports a disabled target as
 * success and does nothing (rule 137) -- the wait belongs here rather than at each call site,
 * where it is one line to forget and the failure it causes names some later assertion.
 */
export async function fill(person: UserEvent, box: HTMLElement, value: string): Promise<void> {
  await waitFor(() => expect(box).toBeEnabled());
  await person.clear(box);
  // `paste` goes to whatever holds focus, which `clear` has just left on the box. An empty
  // value is the clear on its own: user-event refuses a paste of nothing.
  if (value !== "") await person.paste(value);
  // The box really holds it. A paste that lands nowhere is SILENT -- the same way a click on a
  // disabled control is (rule 137) -- and several tests converted to this helper assert that
  // some control stays DISABLED, which an empty box satisfies just as well as the half-typed
  // one they mean. Without this line those tests would keep passing while proving nothing
  // (rules 118, 119). It is also the guard on the helper's own precondition: a box whose
  // component rewrites what it is given needs `type` and its keystrokes, and it fails here
  // rather than somewhere downstream.
  await waitFor(() => expect(box).toHaveValue(value));
}
