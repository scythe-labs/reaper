// SPDX-License-Identifier: AGPL-3.0-or-later
// What a finished scan refreshes. This lives on the shell rather than the scan bar, so a scan
// started from the Reap page's "Scan now", the scheduler, or another device still updates
// whichever page the operator is looking at, including the expired-spares notice, whose whole
// purpose is to go away after a scan. These tests pin the edge it fires on.

import { renderHook } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { createElement, type ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import { Announcer } from "./announce";
import type { ReasonKey } from "./api";
import { testQueryClient } from "./test/queryClient";
import { SCAN_SETTLED_KEYS, useScanSettled } from "./useScanSettled";

function harness() {
  const client = testQueryClient();
  const invalidated: string[] = [];
  vi.spyOn(client, "invalidateQueries").mockImplementation((filters) => {
    invalidated.push(JSON.stringify(filters?.queryKey));
    return Promise.resolve();
  });
  // This is the one provider tree still built by hand, and it stays that way because
  // `renderHookWithProviders` wraps the hook alone. `Announcer` has to be a sibling above the
  // hook, since `announce()` returns early when no region is listening. Without it, the hook's
  // sentence is dropped and a test about it would pass against silence. The app mounts
  // `Announcer` above every route, so this is the shipped arrangement.
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client }, createElement(Announcer), children);
  const view = renderHook(
    ({ scanning, error }: { scanning: boolean; error?: ReasonKey | null }) =>
      useScanSettled(scanning, error),
    {
      initialProps: { scanning: false } as { scanning: boolean; error?: ReasonKey | null },
      wrapper,
    },
  );
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
    // These keys are named outright, because the assertion above cannot reach a key that is
    // missing from the list. It compares the list to itself, so it pins the order and the
    // firing, and it would read green for a surface nobody added. Every one of these keys has
    // actually been missing from the list at some point.
    //
    // The Reap page's ledger is the one this batch exists for.
    expect(invalidated).toContain(JSON.stringify(["reap-breakdown"]));
    // The policy editor's warnings, once refreshed from a second copy of this effect inside
    // `PolicyEditor` while this list claimed to be the single place.
    expect(invalidated).toContain(JSON.stringify(["validate"]));
    // The Jobs page's shelf row, which a scan updates on its way out. Its own "Update now"
    // button used to be the only thing that refreshed it, so the counts under "Runs after
    // every scan" were the previous pass's for as long as the panel stayed mounted.
    expect(invalidated).toContain(JSON.stringify(["leaving-soon-settings"]));
    // Plex's recorded-watch-history line, whose sentence names the last scan while quoting a
    // number that only a scan moves.
    expect(invalidated).toContain(JSON.stringify(["watch-evidence"]));
  });

  it("does nothing on a mount that arrives after the scan already ended", () => {
    // Otherwise every navigation would re-invalidate what it had just fetched. The transition
    // is the signal, not the value.
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
  // Every cache in `SCAN_SETTLED_KEYS` goes stale at once here, which is most of the numbers in
  // the app moving. The trigger can be the scheduler, another device, or another tab, so there
  // is no press to hang a sentence on and no control that visibly changed. This transition is
  // entirely visual. A count written in prose beside the list it counts goes stale the next
  // time an entry is appended, so this comment does not state one.
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
    // This keeps the same edge discipline as the invalidation. A navigation must not announce
    // a scan that finished before this hook existed.
    harness();

    expect(spoken()).toBe("");
  });

  it("says a crashed scan stopped, instead of reporting numbers it did not update", () => {
    // `api/scan.py` clears `running` inside a `finally`, so a scan that crashed arrives at this
    // exact edge carrying its message. Without this branch, the sentence above would tell the
    // operator a scan had finished and the page had been updated, when nothing had, since no
    // snapshot is written by a run that crashed. This hook is the only voice on every page
    // except the one mounting the scan bar, so nothing else would correct it.
    const { view } = harness();

    view.rerender({ scanning: true });
    view.rerender({
      scanning: false,
      error: { k: "error.scan.source_unreachable", p: { error: "Sonarr timed out" } },
    });

    expect(spoken()).toContain("stopped before it finished");
    // This is named outright rather than left to the sentence above. The two branches must not
    // be able to converge on wording that claims an update. "finished" alone cannot
    // discriminate them, since the failure sentence contains that word too.
    expect(spoken()).not.toContain("have been updated");
  });

  it("still refreshes the caches when a scan crashes", () => {
    // A run that stopped somewhere unknown is exactly when a stale number on screen is worst,
    // and a refetch that comes back unchanged costs nothing. The branch is the sentence, not
    // the refresh.
    const { view, invalidated } = harness();

    view.rerender({ scanning: true });
    view.rerender({
      scanning: false,
      error: { k: "error.scan.source_unreachable", p: { error: "Sonarr timed out" } },
    });

    expect(invalidated).toEqual(SCAN_SETTLED_KEYS.map((k) => JSON.stringify(k)));
  });

  it("names no count, because the refetches it just triggered have not landed", () => {
    // A figure stated here would be the previous scan's, since this hook marks caches stale
    // and never reads them. The sentence is deliberately about the change, not about a number.
    const { view } = harness();
    view.rerender({ scanning: true });
    view.rerender({ scanning: false });

    expect(spoken()).not.toMatch(/\d/);
  });
});
