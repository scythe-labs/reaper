// SPDX-License-Identifier: AGPL-3.0-or-later
// What a finished scan refreshes. This used to live in the scan bar, which only Settings
// mounts, so a scan started from the Reap page's "Scan now" (or the scheduler, or another
// device) ended with the page in front of the operator still quoting the previous snapshot --
// the expired-spares notice included, which is the one line whose whole purpose is to go away
// after a scan. It belongs on the shell, and these pin the edge it fires on.

import { renderHook } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createElement, type ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import { SCAN_SETTLED_KEYS, useScanSettled } from "./useScanSettled";

function harness() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const invalidated: string[] = [];
  vi.spyOn(client, "invalidateQueries").mockImplementation((filters) => {
    invalidated.push(JSON.stringify(filters?.queryKey));
    return Promise.resolve();
  });
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client }, children);
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
    // The Reap page's ledger is the one this batch exists for.
    expect(invalidated).toContain(JSON.stringify(["reap-breakdown"]));
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
