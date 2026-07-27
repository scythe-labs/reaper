// SPDX-License-Identifier: AGPL-3.0-or-later
// The settings shell has two forms of the same navigation: a rail of nine tabs on a wide screen,
// one picker below NARROW_SCREEN_QUERY. jsdom has no `matchMedia`, so `useMediaQuery` reports
// false and every other suite in this tree exercises the rail; the picker is only reachable with
// the query stubbed, which is what these do. Only one of the two is ever rendered, so each test
// also asserts the absence of the other -- a CSS-hidden twin would leave both in the tree.
import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { testQueryClient } from "../test/queryClient";
import { Settings } from "./Settings";

const { apiMock } = vi.hoisted(() => ({
  apiMock: { about: vi.fn(), safety: vi.fn() },
}));

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  api: apiMock,
}));

const PANELS = [
  "General",
  "Services",
  "Plex",
  "Jobs",
  "Notifications",
  "Security",
  "Backup & Restore",
  "Logs",
  "About",
];

/** Report `matches` for every query asked, the way a phone would for the narrow one. */
function stubMatchMedia(matches: boolean) {
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches,
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  }));
}

beforeEach(() => {
  vi.clearAllMocks();
  apiMock.about.mockResolvedValue({
    version: "0.0.0-test",
    license: "AGPL-3.0-or-later",
    data_dir: "/data",
    reaper_db_bytes: 1024,
    cache_db_bytes: 1024,
  });
  apiMock.safety.mockResolvedValue({ armed: false, has_password: true });
});

// Rule 133: the stub is process-global, so it never outlives its own test.
afterEach(() => {
  vi.unstubAllGlobals();
});

function renderSettings() {
  const queryClient = testQueryClient();
  render(
    <QueryClientProvider client={queryClient}>
      <Settings initialPanel="about" />
    </QueryClientProvider>,
  );
  return userEvent.setup();
}

describe("the settings section navigation", () => {
  it("is a rail of every panel on a wide screen", async () => {
    stubMatchMedia(false);
    renderSettings();

    const rail = await waitFor(() => {
      const el = document.querySelector(".settings-nav");
      expect(el).not.toBeNull();
      return el!;
    });
    expect([...rail.querySelectorAll("button")].map((b) => b.textContent)).toEqual(PANELS);
    // The panel being read is stated, not only colored.
    expect(rail.querySelector('[aria-current="page"]')!.textContent).toBe("About");
    expect(document.querySelector(".settings-picker")).toBeNull();
  });

  it("is one picker on a narrow screen, carrying the same panels", async () => {
    stubMatchMedia(true);
    renderSettings();

    const picker = (await screen.findByLabelText("Settings section")) as HTMLSelectElement;
    expect([...picker.options].map((o) => o.textContent)).toEqual(PANELS);
    // It names where you are, which is the job the rail's active tab was doing.
    expect(picker.value).toBe("about");
    expect(document.querySelector(".settings-nav")).toBeNull();
  });

  it("switches panel from the picker", async () => {
    stubMatchMedia(true);
    const person = renderSettings();

    const picker = await screen.findByLabelText("Settings section");
    // Rule 137: user-event reports a disabled target as success, so act only once it is usable.
    await waitFor(() => expect(picker).toBeEnabled());
    await person.selectOptions(picker, "security");

    expect(await screen.findByRole("heading", { name: "Security" })).toBeInTheDocument();
    expect(picker).toHaveValue("security");
  });
});
