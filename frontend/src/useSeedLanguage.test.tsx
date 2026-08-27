// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The language seed: the one moment the browser's own preference reaches the server.
//
// The setting lives on the server because a notification is composed there, with no browser to
// ask. But nothing before sign-in can carry a browser's answer, since `AuthGuard` opens only
// `/api/health` and `/api/auth/`. So the first authenticated read is where this fires. Without
// it, an upgraded install would show one language in the picker while writing notifications in
// another.

import { waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { DEFAULT_GENERAL } from "./test/apiFixtures";
import { renderHookWithProviders } from "./test/renderWithProviders";
import { useSeedLanguage } from "./useGeneralSettings";

const { apiMock } = await vi.hoisted(async () => ({
  apiMock: (await import("./test/apiMock")).makeApiMock(),
}));

vi.mock("./api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./api")>()),
  api: apiMock,
}));

// What this browser would ask for. Mocked rather than driven through `navigator.languages`,
// which jsdom does not let a test set per case.
vi.mock("./i18n", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./i18n")>()),
  preferredLanguage: () => "es",
}));

beforeEach(() => {
  vi.clearAllMocks();
});

describe("the language seed", () => {
  it("writes this browser's language when the server has none", async () => {
    apiMock.general.mockResolvedValue({ ...DEFAULT_GENERAL, language: null });
    apiMock.saveGeneral.mockResolvedValue({ ...DEFAULT_GENERAL, language: "es" });

    renderHookWithProviders(() => useSeedLanguage());

    await waitFor(() => expect(apiMock.saveGeneral).toHaveBeenCalledTimes(1));
    expect(apiMock.saveGeneral.mock.calls[0]![0]).toEqual({ language: "es" });
  });

  it("leaves a stored language alone", async () => {
    apiMock.general.mockResolvedValue({ ...DEFAULT_GENERAL, language: "en" });

    renderHookWithProviders(() => useSeedLanguage());

    // Settled, not merely "not yet": wait for the read the effect keys on to land, so this
    // cannot pass by asserting before anything had a chance to fire.
    await waitFor(() => expect(apiMock.general).toHaveBeenCalled());
    expect(apiMock.saveGeneral).not.toHaveBeenCalled();
  });

  it("writes once, however many times it re-renders", async () => {
    apiMock.general.mockResolvedValue({ ...DEFAULT_GENERAL, language: null });
    // Never resolves, so the write is still in flight across every re-render below. A latch
    // set on the response rather than on the request would fire again here, which is the
    // shape that would seed twice on a slow network.
    apiMock.saveGeneral.mockReturnValue(new Promise(() => {}));

    const { rerender } = renderHookWithProviders(() => useSeedLanguage());

    await waitFor(() => expect(apiMock.saveGeneral).toHaveBeenCalledTimes(1));
    rerender();
    rerender();
    expect(apiMock.saveGeneral).toHaveBeenCalledTimes(1);
  });

  it("says nothing when the write is refused", async () => {
    apiMock.general.mockResolvedValue({ ...DEFAULT_GENERAL, language: null });
    apiMock.saveGeneral.mockRejectedValue(new Error("nope"));

    renderHookWithProviders(() => useSeedLanguage());

    // The app is already painting in this language and Settings can still set it, so a failed
    // seed is not worth a screen. It must not become an unhandled rejection either.
    await waitFor(() => expect(apiMock.saveGeneral).toHaveBeenCalledTimes(1));
  });
});
