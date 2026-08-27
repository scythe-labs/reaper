// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The remembered review-queue filters: per-tab, sanitized on the way back in. A stored
// value from an older build (or a hand-edited one) must degrade field by field to the
// defaults, never crash the queue or smuggle in an impossible filter state.
//
// This environment's jsdom ships without storage (window.localStorage is undefined), so the
// tests install a faithful in-memory stand-in. The helpers under test also survive that
// absence on their own, which "defaults when storage is unusable" exercises.

import { beforeEach, describe, expect, it } from "vitest";
import { DEFAULT_FILTERS, loadFilters, saveFilters, type QueueFilters } from "./ReviewQueue";

const store = new Map<string, string>();

function installStorage(): void {
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    value: {
      getItem: (key: string) => store.get(key) ?? null,
      setItem: (key: string, value: string) => void store.set(key, String(value)),
      removeItem: (key: string) => void store.delete(key),
      clear: () => store.clear(),
    },
  });
}

describe("remembered filters", () => {
  beforeEach(() => {
    store.clear();
    installStorage();
  });

  it("returns the defaults when nothing is stored", () => {
    expect(loadFilters("condemn")).toEqual(DEFAULT_FILTERS);
  });

  it("round-trips what was saved, per tab", () => {
    const chosen: QueueFilters = {
      mediaType: "season",
      requested: "yes",
      genre: "Comedy",
      library: "4K Movies",
      override: "spare",
      sort: "size",
      order: "asc",
    };
    saveFilters("condemn", chosen);
    expect(loadFilters("condemn")).toEqual(chosen);
    // The other tabs are untouched: each remembers its own.
    expect(loadFilters("protect")).toEqual(DEFAULT_FILTERS);
  });

  it("drops an unknown stored value back to that field's default only", () => {
    store.set(
      "reaper.queue.filters.condemn",
      JSON.stringify({ mediaType: "cassette", genre: "Horror", override: "spare" }),
    );
    const loaded = loadFilters("condemn");
    expect(loaded.mediaType).toBe(DEFAULT_FILTERS.mediaType); // unknown -> default
    expect(loaded.genre).toBe("Horror"); // the valid fields survive
    expect(loaded.override).toBe("spare");
  });

  it("survives garbage in storage", () => {
    store.set("reaper.queue.filters.condemn", "not json");
    expect(loadFilters("condemn")).toEqual(DEFAULT_FILTERS);
  });

  it("returns the defaults when storage is unusable", () => {
    Object.defineProperty(window, "localStorage", { configurable: true, value: undefined });
    expect(loadFilters("condemn")).toEqual(DEFAULT_FILTERS);
    // And saving is a quiet no-op, never a crash.
    expect(() => saveFilters("condemn", DEFAULT_FILTERS)).not.toThrow();
  });
});
