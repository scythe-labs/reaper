// SPDX-License-Identifier: AGPL-3.0-or-later
// What a finished scan refreshes. This used to live in the scan bar, which only Settings
// mounts, so a scan started from the Reap page's "Scan now" (or the scheduler, or another
// device) ended with the page in front of the operator still quoting the previous snapshot --
// the expired-spares notice included, which is the one line whose whole purpose is to go away
// after a scan. It belongs on the shell, and these pin the edge it fires on.

import { renderHook } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { createElement, type ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import { Announcer } from "./announce";
import { testQueryClient } from "./test/queryClient";
import { SCAN_SETTLED_KEYS, useScanSettled } from "./useScanSettled";

function harness() {
  const client = testQueryClient();
  const invalidated: string[] = [];
  vi.spyOn(client, "invalidateQueries").mockImplementation((filters) => {
    invalidated.push(JSON.stringify(filters?.queryKey));
    return Promise.resolve();
  });
  // `Announcer` inside the wrapper because `announce()` returns early when no region is
  // listening -- without it the hook's sentence is dropped and a test about it passes against
  // silence. The app mounts it above every route, so this is the shipped arrangement.
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client }, createElement(Announcer), children);
  const view = renderHook(({ scanning }) => useScanSettled(scanning), {
    initialProps: { scanning: false },
    wrapper,
  });
  return { view, invalidated };
}

describe("useScanSettled", () => {
  it("refreshes every snapshot-backed cache when a scan ends", () => {
    const { view, invalidated } = harness();

    view.rerender({ scanning: true });
    expect(invalidated).toEqual([]);
    view.rerender({ scanning: false });

    // The whole list, so a surface added to it later is covered by this test rather than
    // discovered missing by an operator reading a stale number.
    expect(invalidated).toEqual(SCAN_SETTLED_KEYS.map((k) => JSON.stringify(k)));
    // Two keys named outright, because the assertion above cannot reach a key that is missing
    // from the list: it compares the list to itself, so it pins the ORDER and the firing, and
    // reads green for a surface nobody added (rule 141). These are the two whose absence has
    // actually happened.
    //
    // The Reap page's ledger is the one this batch exists for.
    expect(invalidated).toContain(JSON.stringify(["reap-breakdown"]));
    // The policy editor's warnings, refreshed from a second copy of this effect inside
    // `PolicyEditor` while this list claimed to be the single place (#205).
    expect(invalidated).toContain(JSON.stringify(["validate"]));
  });

  it("does nothing on a mount that arrives after the scan already ended", () => {
    // Otherwise every navigation re-invalidated what it had just fetched. The transition is
    // the signal, not the value.
    const { invalidated } = harness();
    expect(invalidated).toEqual([]);
  });

  it("fires once per scan, not on every idle render", () => {
    const { view, invalidated } = harness();

    view.rerender({ scanning: true });
    view.rerender({ scanning: false });
    const afterFirst = invalidated.length;
    view.rerender({ scanning: false });
    view.rerender({ scanning: false });

    expect(invalidated.length).toBe(afterFirst);
  });
});

describe("what a background scan tells an operator using a screen reader", () => {
  // Thirteen caches go stale at once here, which is most of the numbers in the app moving -- and
  // the trigger can be the scheduler, another device, or another tab, so there is no press to
  // hang a sentence on and no control that changed. The transition was entirely visual (#177).
  const spoken = () =>
    [...document.querySelectorAll('[aria-live="polite"]')].map((n) => n.textContent).join("");

  it("says the numbers moved, on the same edge the refresh fires on", () => {
    const { view } = harness();

    view.rerender({ scanning: true });
    expect(spoken()).toBe("");

    view.rerender({ scanning: false });

    expect(spoken()).toContain("A scan finished.");
  });

  it("says nothing on a mount that arrives after the scan already ended", () => {
    // Same edge discipline the invalidation keeps: a navigation must not announce a scan that
    // finished before this hook existed.
    harness();

    expect(spoken()).toBe("");
  });

  it("names no count, because the refetches it just triggered have not landed", () => {
    // A figure said here would be the PREVIOUS scan's -- this hook marks caches stale and never
    // reads them (rule 85). The sentence is deliberately about the change, not about a number.
    const { view } = harness();
    view.rerender({ scanning: true });
    view.rerender({ scanning: false });

    expect(spoken()).not.toMatch(/\d/);
  });
});
