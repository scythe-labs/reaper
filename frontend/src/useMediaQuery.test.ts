// SPDX-License-Identifier: AGPL-3.0-or-later
// The JS<->CSS breakpoint bridge behind App's scroll-keeping (it decides whether the panel is a
// full-screen sheet on a phone or a side column on a wider screen). Two things must hold: it
// reports the query's state and follows it across a change, and where matchMedia is absent
// (jsdom here, and any engine too old to have it) it reports false rather than throwing, so the
// caller falls back to the wide-screen branch instead of the phone one.
import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useMediaQuery } from "./useMediaQuery";

afterEach(() => {
  vi.unstubAllGlobals();
});

/** A minimal MediaQueryList whose `matches` can be flipped, firing the change listeners the
 *  hook subscribes with -- the shape our hook actually touches, nothing more. */
function stubMatchMedia(initial: boolean) {
  let matches = initial;
  const listeners = new Set<() => void>();
  const mql = {
    get matches() {
      return matches;
    },
    media: "",
    addEventListener: (_: string, cb: () => void) => listeners.add(cb),
    removeEventListener: (_: string, cb: () => void) => listeners.delete(cb),
  };
  vi.stubGlobal("matchMedia", (q: string) => {
    mql.media = q;
    return mql;
  });
  return {
    set(next: boolean) {
      matches = next;
      listeners.forEach((cb) => cb());
    },
  };
}

describe("useMediaQuery", () => {
  it("reports the query's current match", () => {
    stubMatchMedia(true);
    const { result } = renderHook(() => useMediaQuery("(max-width: 900px)"));
    expect(result.current).toBe(true);
  });

  it("follows the query as the viewport crosses it", () => {
    const ctl = stubMatchMedia(false);
    const { result } = renderHook(() => useMediaQuery("(max-width: 900px)"));
    expect(result.current).toBe(false);
    act(() => ctl.set(true));
    expect(result.current).toBe(true);
  });

  it("reports false where matchMedia is unavailable", () => {
    vi.stubGlobal("matchMedia", undefined);
    const { result } = renderHook(() => useMediaQuery("(max-width: 900px)"));
    expect(result.current).toBe(false);
  });
});
