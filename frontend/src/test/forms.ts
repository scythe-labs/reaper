// SPDX-License-Identifier: AGPL-3.0-or-later
// Puts a value in a text box the way a test means it: as one edit, not as many keystrokes.
//
// `userEvent.type(box, "https://reaper.example.com")` dispatches one event per character, and
// every one of them re-renders the panel around the box. That cost is per keystroke, so under
// load the slowest test in a family is the one that typed the most, and a long enough typed
// value can push a test past vitest's default timeout with no real bug behind it.
//
// What makes this the fix rather than a bigger timeout is the tail. Measured over several runs,
// `type` had a median of 116ms and a worst case of 405ms. The same drafts filled through here
// measured 37ms median and 39ms worst. One event has almost no spread to have, so the number
// that decides whether a run goes red stops moving.
//
// Use it wherever the typing is setup, where the test asserts on the state the form ends in.
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
 * Waits for the box to be enabled first, because user-event reports a disabled target as a
 * success and does nothing. The wait belongs here rather than at each call site, where it is
 * one line to forget and the failure it causes names some later assertion.
 */
export async function fill(person: UserEvent, box: HTMLElement, value: string): Promise<void> {
  await waitFor(() => expect(box).toBeEnabled());
  await person.clear(box);
  // `paste` goes to whatever holds focus, which `clear` has just left on the box. An empty
  // value is the clear on its own, since user-event refuses a paste of nothing.
  if (value !== "") await person.paste(value);
  // Confirms the box really holds it. A paste that lands nowhere is silent, the same way a
  // click on a disabled control is, and several tests using this helper assert that some
  // control stays disabled, which an empty box satisfies just as well as the half-typed one
  // they mean. Without this check, those tests would keep passing while proving nothing. It is
  // also the guard on the helper's own precondition: a box whose component rewrites what it is
  // given needs `type` and its keystrokes, and it fails here rather than somewhere downstream.
  await waitFor(() => expect(box).toHaveValue(value));
}
