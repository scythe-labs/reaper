// SPDX-License-Identifier: AGPL-3.0-or-later
// Deletion is armed in the database and stays armed, so this is the one piece of state that
// changes without the tab doing anything -- and the one the tab must not be caught out on.
import { QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { testQueryClient } from "./test/queryClient";
import { useSafety } from "./useSafety";

const { apiMock } = vi.hoisted(() => ({ apiMock: { safety: vi.fn() } }));

vi.mock("./api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./api")>()),
  api: apiMock,
}));

beforeEach(() => {
  // `shouldAdvanceTime` keeps waitFor's own polling alive while the clock is ours to move.
  vi.useFakeTimers({ shouldAdvanceTime: true });
  apiMock.safety.mockResolvedValue({ destructive_enabled: false, has_password: true, note: null });
});
afterEach(() => vi.useRealTimers());

describe("the safety state", () => {
  it("re-reads itself on a clock, so arming elsewhere reaches this tab", async () => {
    // B-9: with no interval and the app-wide `refetchOnWindowFocus: false`, a desktop tab left
    // on Review went on saying "Reaper can look but can't remove anything" for as long as it
    // was open after deletion was armed from a phone. That is the fail-open direction on the
    // app's one always-visible safety surface.
    const client = testQueryClient();
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client }, children);
    const { result } = renderHook(() => useSafety(), { wrapper });

    await waitFor(() => expect(result.current.data?.destructive_enabled).toBe(false));
    expect(apiMock.safety).toHaveBeenCalledTimes(1);

    // Armed from somewhere else. Nothing happens in this tab at all.
    apiMock.safety.mockResolvedValue({
      destructive_enabled: true,
      has_password: true,
      note: null,
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(15_000);
    });

    await waitFor(() => expect(result.current.data?.destructive_enabled).toBe(true));
  });
});
