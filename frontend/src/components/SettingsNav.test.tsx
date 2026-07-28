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
import { DEFAULT_GENERAL, seedSettings } from "../test/apiFixtures";
import { testQueryClient } from "../test/queryClient";
import { Settings } from "./Settings";

const { apiMock } = vi.hoisted(() => ({
  apiMock: { about: vi.fn(), safety: vi.fn(), general: vi.fn(), saveGeneral: vi.fn() },
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
  apiMock.general.mockResolvedValue(DEFAULT_GENERAL);
  apiMock.saveGeneral.mockResolvedValue(DEFAULT_GENERAL);
});

// Rule 133: the stub is process-global, so it never outlives its own test.
afterEach(() => {
  vi.unstubAllGlobals();
});

function renderSettings(initialPanel: "about" | "general" = "about") {
  // Seeded, not just mocked: the General panel renders its fields from this read, and a mocked
  // answer lands a microtask later -- after a synchronous assertion, which would then be about
  // the "Loading…" panel rather than the one an operator types into (rule 136).
  const queryClient = seedSettings(testQueryClient());
  render(
    <QueryClientProvider client={queryClient}>
      <Settings initialPanel={initialPanel} />
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

describe("leaving General with something unsaved", () => {
  // The save bar holds up to six fields at once and switching section unmounts the panel holding
  // them. Both ways of switching have to stop and ask, which is why the picker is tested here
  // too: fixing one call site and leaving its twin is rule 72's failure.
  const warning = () => document.querySelector(".notice-warn");
  const heading = () => screen.getByRole("heading", { level: 2 }).textContent;

  async function typeADraft(person: ReturnType<typeof userEvent.setup>) {
    const url = await screen.findByLabelText("Application URL");
    await waitFor(() => expect(url).toBeEnabled());
    await person.type(url, "https://reaper.example.com");
    // The bar is the definition of a draft, so wait for it rather than for the keystrokes.
    await waitFor(() => expect(document.querySelector(".savebar")).not.toBeNull());
    return url;
  }

  it("holds the switch and says what leaving would cost", async () => {
    stubMatchMedia(false);
    const person = renderSettings("general");
    const url = await typeADraft(person);

    await person.click(screen.getByRole("button", { name: "Security" }));

    expect(warning()!.textContent).toContain(
      "You have unsaved General settings. Switching to Security discards them.",
    );
    // Nothing moved: still General, still the draft, still offered by the bar.
    expect(heading()).toBe("General");
    expect(url).toHaveValue("https://reaper.example.com");
    expect(document.querySelector(".savebar")!.textContent).toContain("Application URL");
  });

  it("goes back to the draft on Keep editing", async () => {
    stubMatchMedia(false);
    const person = renderSettings("general");
    const url = await typeADraft(person);

    await person.click(screen.getByRole("button", { name: "Security" }));
    await person.click(screen.getByRole("button", { name: "Keep editing" }));

    expect(warning()).toBeNull();
    expect(heading()).toBe("General");
    expect(url).toHaveValue("https://reaper.example.com");
  });

  it("leaves only when Discard and switch is pressed", async () => {
    stubMatchMedia(false);
    const person = renderSettings("general");
    await typeADraft(person);

    await person.click(screen.getByRole("button", { name: "Security" }));
    await person.click(screen.getByRole("button", { name: "Discard and switch" }));

    expect(await screen.findByRole("heading", { name: "Security" })).toBeInTheDocument();
    expect(warning()).toBeNull();
    // The draft was discarded, not saved: nothing was written on the way out.
    expect(apiMock.saveGeneral).not.toHaveBeenCalled();
  });

  it("holds the switch from the narrow-screen picker too", async () => {
    stubMatchMedia(true);
    const person = renderSettings("general");
    await typeADraft(person);

    const picker = screen.getByLabelText("Settings section");
    await waitFor(() => expect(picker).toBeEnabled());
    await person.selectOptions(picker, "security");

    expect(warning()).not.toBeNull();
    expect(heading()).toBe("General");
    // The picker reports where you actually are, which is still General.
    expect(picker).toHaveValue("general");
  });

  it("switches straight through when there is nothing to lose", async () => {
    stubMatchMedia(false);
    const person = renderSettings("general");
    await screen.findByLabelText("Application URL");

    await person.click(screen.getByRole("button", { name: "Security" }));

    expect(warning()).toBeNull();
    expect(await screen.findByRole("heading", { name: "Security" })).toBeInTheDocument();
  });

  it("stops warning once the draft is discarded", async () => {
    // The notice exists only because there are edits to lose. `PolicyEditor` shipped this bug in
    // its own copy: the warning survived a Discard and went on offering to throw away changes
    // that no longer existed.
    stubMatchMedia(false);
    const person = renderSettings("general");
    await typeADraft(person);

    await person.click(screen.getByRole("button", { name: "Security" }));
    expect(warning()).not.toBeNull();
    await person.click(screen.getByRole("button", { name: "Discard" }));

    await waitFor(() => expect(warning()).toBeNull());
    expect(heading()).toBe("General");
  });
});
