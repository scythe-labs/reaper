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
  apiMock: {
    about: vi.fn(),
    safety: vi.fn(),
    general: vi.fn(),
    saveGeneral: vi.fn(),
    // Plex and Notifications hold drafts of their own, so the switch guard is exercised on
    // those panels too and both trees mount here. Rule 135: a module mock answers everything
    // the tree under test reads, including the reads no test in the file names.
    plexStatus: vi.fn(),
    plexResources: vi.fn(),
    plexLibraries: vi.fn(),
    syncPlexLibraries: vi.fn(),
    setPlexLibraries: vi.fn(),
    leavingSoonSettings: vi.fn(),
    setLeavingSoonSettings: vi.fn(),
    syncLeavingSoon: vi.fn(),
    setPlexWebUrl: vi.fn(),
    plexSetConnection: vi.fn(),
    plexSwitchServer: vi.fn(),
    plexUnlink: vi.fn(),
    plexLinkStart: vi.fn(),
    plexLinkPoll: vi.fn(),
    notifications: vi.fn(),
    setWebhook: vi.fn(),
    testWebhook: vi.fn(),
    clearWebhook: vi.fn(),
  },
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

/** The stored Plex web address, and so the value the box has to DIVERGE from to be a draft. */
const SAVED_WEB_URL = "https://app.plex.tv";

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
  apiMock.plexStatus.mockResolvedValue({
    linked: true,
    name: "Example server",
    connection_uri: "https://plex.example.net:32400",
    last_ok_at: null,
    verify_tls: true,
    web_url: SAVED_WEB_URL,
  });
  apiMock.plexResources.mockResolvedValue({ source: "plex.tv", servers: [] });
  apiMock.plexLibraries.mockResolvedValue([]);
  apiMock.leavingSoonSettings.mockResolvedValue({
    enabled: false,
    allow_unarmed: false,
    last: null,
  });
  apiMock.notifications.mockResolvedValue({ has_webhook: false });
});

// Rule 133: the stub is process-global, so it never outlives its own test.
afterEach(() => {
  vi.unstubAllGlobals();
});

function renderSettings(initialPanel: "about" | "general" | "plex" | "notifications" = "about") {
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

describe("leaving Plex or Notifications with something unsaved", () => {
  // The guard first landed on General alone, and these two kept typed drafts behind their own
  // inline Saves -- so the app asked on one panel and threw the draft away without a word on the
  // next two (rule 72). Driven through the real shell rather than against the panels' props,
  // because what broke was the wiring between them, which a prop-level test cannot see.
  //
  // Scoped to the notice that names a switch: the Plex panel renders `.notice-warn` of its own
  // (the certificate one), so a bare `.notice-warn` lookup would pass on the wrong element.
  const switchNotice = () =>
    [...document.querySelectorAll(".notice-warn")].find((n) =>
      n.textContent?.includes("Switching to"),
    ) ?? null;
  const heading = () => screen.getByRole("heading", { level: 2 }).textContent;

  it("holds the switch on a half-typed Discord webhook", async () => {
    stubMatchMedia(false);
    const person = renderSettings("notifications");
    const box = await screen.findByLabelText("Discord webhook URL");
    await waitFor(() => expect(box).toBeEnabled());
    await person.type(box, "https://discord.com/api/webhooks/1/secret");

    await person.click(screen.getByRole("button", { name: "Security" }));

    expect(switchNotice()!.textContent).toContain(
      "You have unsaved Notifications settings. Switching to Security discards them.",
    );
    // Nothing moved, and the secret is still in the box to be saved.
    expect(heading()).toBe("Notifications");
    expect(box).toHaveValue("https://discord.com/api/webhooks/1/secret");
  });

  it("holds the switch on a webhook too malformed to save", async () => {
    // The report is "there is something to lose", not "there is something Save would accept".
    // Reporting only the valid form would drop a mis-pasted secret silently, which is the one
    // draft in Settings the operator cannot recover from anywhere but Discord.
    stubMatchMedia(false);
    const person = renderSettings("notifications");
    const box = await screen.findByLabelText("Discord webhook URL");
    await waitFor(() => expect(box).toBeEnabled());
    await person.type(box, "discord.com/api/webhoo");

    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    await person.click(screen.getByRole("button", { name: "Security" }));

    expect(switchNotice()).not.toBeNull();
    expect(heading()).toBe("Notifications");
  });

  it("switches straight through when the webhook box is empty", async () => {
    stubMatchMedia(false);
    const person = renderSettings("notifications");
    await screen.findByLabelText("Discord webhook URL");

    await person.click(screen.getByRole("button", { name: "Security" }));

    expect(switchNotice()).toBeNull();
    expect(await screen.findByRole("heading", { name: "Security" })).toBeInTheDocument();
  });

  it("holds the switch on an edited Plex web address", async () => {
    stubMatchMedia(false);
    const person = renderSettings("plex");
    // No accessible label on this row (a `.set-label` span, not a `<label>`), so it is reached
    // the way an operator sees it. Rule 137: the panel early-returns "Loading…" until the status
    // read lands, so wait for the box to be usable rather than for the page.
    const box = await screen.findByPlaceholderText(SAVED_WEB_URL);
    await waitFor(() => expect(box).toBeEnabled());
    await person.clear(box);
    await person.type(box, "https://plex.example.net");

    await person.click(screen.getByRole("button", { name: "Security" }));

    expect(switchNotice()!.textContent).toContain(
      "You have unsaved Plex settings. Switching to Security discards them.",
    );
    expect(heading()).toBe("Plex");
    expect(box).toHaveValue("https://plex.example.net");
  });

  it("leaves Plex only when Discard and switch is pressed, and saves nothing on the way", async () => {
    stubMatchMedia(false);
    const person = renderSettings("plex");
    const box = await screen.findByPlaceholderText(SAVED_WEB_URL);
    await waitFor(() => expect(box).toBeEnabled());
    await person.clear(box);
    await person.type(box, "https://plex.example.net");

    await person.click(screen.getByRole("button", { name: "Security" }));
    await person.click(screen.getByRole("button", { name: "Discard and switch" }));

    expect(await screen.findByRole("heading", { name: "Security" })).toBeInTheDocument();
    expect(switchNotice()).toBeNull();
    expect(apiMock.setPlexWebUrl).not.toHaveBeenCalled();
  });

  it("switches straight through when the Plex web address still matches the stored one", async () => {
    stubMatchMedia(false);
    const person = renderSettings("plex");
    const box = await screen.findByPlaceholderText(SAVED_WEB_URL);
    await waitFor(() => expect(box).toBeEnabled());
    // Typed and put back: the box diverged and returned, so there is nothing left to lose.
    await person.clear(box);
    await person.type(box, SAVED_WEB_URL);

    await person.click(screen.getByRole("button", { name: "Security" }));

    expect(switchNotice()).toBeNull();
    expect(await screen.findByRole("heading", { name: "Security" })).toBeInTheDocument();
  });
});
