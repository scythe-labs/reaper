// SPDX-License-Identifier: AGPL-3.0-or-later
// Deletion is armed in the database and stays armed, so this is the one piece of state that
// changes without the tab doing anything. It is also the one state the tab must not be caught
// out on.
import { act, waitFor } from "@testing-library/react";
import { renderHookWithProviders } from "./test/renderWithProviders";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useSafety } from "./useSafety";

const { apiMock } = await vi.hoisted(async () => ({
  apiMock: (await import("./test/apiMock")).makeApiMock(),
}));

vi.mock("./api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./api")>()),
  api: apiMock,
}));

beforeEach(() => {
  // `shouldAdvanceTime` keeps waitFor's own polling alive while the clock is ours to move.
  vi.useFakeTimers({ shouldAdvanceTime: true });
  apiMock.safety.mockResolvedValue({
    destructive_enabled: false,
    has_password: true,
  });
});
afterEach(() => vi.useRealTimers());

describe("the safety state", () => {
  it("re-reads itself on a clock, so arming elsewhere reaches this tab", async () => {
    // Without a polling interval, and with the app-wide `refetchOnWindowFocus: false`, a
    // desktop tab left on Review would go on saying "Reaper can look but can't remove anything"
    // indefinitely after deletion was armed from a phone. That direction is unsafe on the app's
    // one always-visible safety surface.
    const { result } = renderHookWithProviders(() => useSafety());

    await waitFor(() => expect(result.current.data?.destructive_enabled).toBe(false));
    expect(apiMock.safety).toHaveBeenCalledTimes(1);

    // Armed from somewhere else. Nothing happens in this tab at all.
    apiMock.safety.mockResolvedValue({
      destructive_enabled: true,
      has_password: true,
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(15_000);
    });

    await waitFor(() => expect(result.current.data?.destructive_enabled).toBe(true));
  });
});
