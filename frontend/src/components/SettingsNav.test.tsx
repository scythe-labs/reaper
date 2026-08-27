// SPDX-License-Identifier: AGPL-3.0-or-later
// The settings shell has two forms of the same navigation: a rail of ten tabs on a wide screen,
// and one picker below NARROW_SCREEN_QUERY. jsdom has no `matchMedia`, so `useMediaQuery`
// reports false and every other suite in this tree exercises the rail. The picker is only
// reachable with the query stubbed, which is what these tests do. Only one of the two is ever
// rendered, so each test also checks that the other is absent, since a CSS-hidden twin would
// leave both in the tree.
import { fireEvent, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { expectNoA11yViolations } from "../test/a11y";
import {
  DEFAULT_GENERAL,
  DEFAULT_UPDATE,
  DEFAULT_WATCH_EVIDENCE,
  SIGNED_IN_USER,
  seedSettings,
} from "../test/apiFixtures";
import { fill } from "../test/forms";
import { testQueryClient } from "../test/queryClient";
import { renderWithProviders } from "../test/renderWithProviders";
import { useState } from "react";
import { panels as declaredPanels, type Panel, Settings } from "./Settings";

const { apiMock } = await vi.hoisted(async () => ({
  apiMock: (await import("../test/apiMock")).makeApiMock(),
}));

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  api: apiMock,
}));

// The spec, written out rather than derived: these are the words an operator reads, so a rename
// has to be typed here too. The test below checks this table against the declaration it
// mirrors. Deriving it instead would assert the rail against itself.
const PANELS = [
  "General",
  "Services",
  "Plex",
  "Lists",
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
  apiMock.update.mockResolvedValue(DEFAULT_UPDATE);
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
  apiMock.lists.mockResolvedValue([]);
  apiMock.watchEvidence.mockResolvedValue(DEFAULT_WATCH_EVIDENCE);
  apiMock.leavingSoonSettings.mockResolvedValue({
    enabled: false,
    allow_unarmed: false,
    name: "Leaving Soon",
    applied_name: "Leaving Soon",
    last: null,
  });
  apiMock.notifications.mockResolvedValue({ has_webhook: false });
  apiMock.setAdminPassword.mockResolvedValue({ ok: true });
  apiMock.me.mockResolvedValue(SIGNED_IN_USER);
  apiMock.backupInfo.mockResolvedValue({
    reaper_db_bytes: 1024,
    last_backup_at: null,
    key_in_backup: true,
    app_version: "test",
    restore_armed: false,
  });
  apiMock.restorePrepare.mockResolvedValue({
    app_version: null,
    created_at: null,
    verdict: "current",
    key_in_backup: true,
    reaper_db_bytes: 1024,
    token: "staged-token",
  });
  apiMock.restoreCancel.mockResolvedValue({ ok: true });
});

// The stub is process-global, so it must never outlive its own test.
afterEach(() => {
  vi.unstubAllGlobals();
});

/** `App` owns which panel is open, so the address bar can name it (`/settings/logs`, navUrl.ts).
 *  This is that owner, so a rail click moves here the way it moves in the app. A test rendering
 *  `Settings` with a fixed `panel` would sit still through every click and prove nothing. */
function SettingsAt({ open, jumper = false }: { open: Panel; jumper?: boolean }) {
  const [panel, setPanel] = useState(open);
  // The user menu's update item, wired the way `App` wires it: it asks for About rather than
  // setting it, so the confirm inside Settings gets to refuse. Rendered only for the tests
  // about that route, so the rail tests keep counting the rail's own buttons.
  const [jump, setJump] = useState<{ panel: Panel; nonce: number } | null>(null);
  return (
    <>
      {jumper && (
        <button onClick={() => setJump((j) => ({ panel: "about", nonce: (j?.nonce ?? 0) + 1 }))}>
          Update available
        </button>
      )}
      <Settings panel={panel} onPanelChange={setPanel} jump={jump} />
    </>
  );
}

function renderSettings(
  initialPanel: "about" | "general" | "plex" | "notifications" | "security" | "backup" = "about",
  jumper = false,
) {
  // Seeded, not just mocked: the General panel renders its fields from this read, and a mocked
  // answer lands a microtask later, after a synchronous assertion would already have run. That
  // assertion would then be about the "Loading…" panel rather than the one an operator types
  // into.
  renderWithProviders(<SettingsAt open={initialPanel} jumper={jumper} />, {
    client: seedSettings(testQueryClient()),
  });
  return userEvent.setup();
}

describe("the settings section navigation", () => {
  // The rail is the only way between the ten settings sections, and it states which one is open
  // rather than only coloring it. An operator who cannot hear that is somewhere in Settings with
  // no way to tell where.
  it("has no accessibility violations", async () => {
    stubMatchMedia(false);
    renderSettings();

    await waitFor(() => {
      const el = document.querySelector(".settings-nav");
      expect(el).not.toBeNull();
      return el!;
    });
    await expectNoA11yViolations();
  });

  it("mirrors the section list declared in Settings.tsx", () => {
    // One set, two hand copies: this table and `panels` in Settings.tsx. A new section can
    // fail only here, on the labels, while still needing to be classified in the switch guard
    // in `dirtyPanels`. The compiler enforces that classification; this test enforces the label
    // list and names the other place a new section must be updated.
    expect(
      declaredPanels().map((p) => p.label),
      "Settings.tsx's `panels` changed. Update this table, and classify the section in " +
        "`dirtyPanels` (Settings.tsx): tsc refuses a missing key, but a `false` written without " +
        "checking drops that section's unsaved edits with no confirm.",
    ).toEqual(PANELS);
  });

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
    // It names where you are, the same job the rail's active tab does.
    expect(picker.value).toBe("about");
    expect(document.querySelector(".settings-nav")).toBeNull();
  });

  it("switches panel from the picker", async () => {
    stubMatchMedia(true);
    const person = renderSettings();

    const picker = await screen.findByLabelText("Settings section");
    // user-event reports a disabled target as success, so act on it only once it is usable.
    await waitFor(() => expect(picker).toBeEnabled());
    await person.selectOptions(picker, "security");

    expect(await screen.findByRole("heading", { name: "Security" })).toBeInTheDocument();
    expect(picker).toHaveValue("security");
  });
});

describe("leaving General with something unsaved", () => {
  // The save bar can hold up to six fields at once, and switching section unmounts the panel
  // holding them. Both ways of switching must stop and ask, which is why the picker is tested
  // here too: fixing one call site and leaving its twin unfixed would still lose the operator's
  // edits.
  const warning = () => document.querySelector(".notice-warn");
  const heading = () => screen.getByRole("heading", { level: 2 }).textContent;

  async function typeADraft(person: ReturnType<typeof userEvent.setup>) {
    const url = await screen.findByLabelText("Application URL");
    // One edit, not many keystrokes: what this family needs is a draft in the box, and typing
    // one character at a time forces a panel re-render per character, which is what `fill` in
    // `src/test/forms.ts` avoids.
    await fill(person, url, "https://reaper.example.com");
    // The bar is the definition of a draft, so wait for it rather than for the edit.
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

  // A third way in: the user menu's update item lives outside Settings, so it asks `App` for a
  // panel rather than Settings itself. Anything typed in the open panel must not be lost by
  // that press, since the click itself says nothing about settings.
  it("holds a jump from outside the page, the same as a rail click", async () => {
    stubMatchMedia(false);
    const person = renderSettings("general", true);
    const url = await typeADraft(person);

    await person.click(screen.getByRole("button", { name: "Update available" }));

    expect(warning()!.textContent).toContain(
      "You have unsaved General settings. Switching to About discards them.",
    );
    expect(heading()).toBe("General");
    expect(url).toHaveValue("https://reaper.example.com");
  });

  it("lets a jump through on Discard and switch", async () => {
    stubMatchMedia(false);
    const person = renderSettings("general", true);
    await typeADraft(person);

    await person.click(screen.getByRole("button", { name: "Update available" }));
    await person.click(screen.getByRole("button", { name: "Discard and switch" }));

    expect(await screen.findByRole("heading", { name: "About" })).toBeInTheDocument();
    expect(warning()).toBeNull();
    expect(apiMock.saveGeneral).not.toHaveBeenCalled();
  });

  it("takes a jump straight through when the panel holds nothing", async () => {
    stubMatchMedia(false);
    const person = renderSettings("general", true);
    await screen.findByLabelText("Application URL");

    await person.click(screen.getByRole("button", { name: "Update available" }));

    expect(warning()).toBeNull();
    expect(await screen.findByRole("heading", { name: "About" })).toBeInTheDocument();
  });

  it("stops warning once the draft is discarded", async () => {
    // The notice must exist only while there are edits to lose. Once a Discard clears them, the
    // warning must not go on offering to throw away changes that no longer exist.
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
  // Plex and Notifications each keep their typed drafts behind their own inline Save button,
  // so the same unsaved-edits guard must cover them too, or the app would ask on one panel and
  // silently drop a draft on these two. Driven through the real shell rather than against the
  // panels' props, since what the guard depends on is the wiring between them, which a
  // prop-level test cannot see.
  //
  // Scoped to the notice that names a switch, because the Plex panel raises a `.notice-warn` of
  // its own against this fixture: `plexResources` answers with an empty server list while the
  // status says linked, so the linked server reads as missing. A bare `.notice-warn` lookup
  // would pass against that one instead. Name the notice this fixture actually renders, not a
  // plausible one. The certificate warning is absent here, since `plexStatus` returns
  // `verify_tls: true`.
  const switchNotice = () =>
    [...document.querySelectorAll(".notice-warn")].find((n) =>
      n.textContent?.includes("Switching to"),
    ) ?? null;
  const heading = () => screen.getByRole("heading", { level: 2 }).textContent;

  it("holds the switch on a half-typed Discord webhook", async () => {
    stubMatchMedia(false);
    const person = renderSettings("notifications");
    const box = await screen.findByLabelText("Discord webhook URL");
    await fill(person, box, "https://discord.com/api/webhooks/1/secret");

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
    await fill(person, box, "discord.com/api/webhoo");

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
    // the way an operator sees it. The panel early-returns "Loading…" until the status read
    // lands, so wait for the box to be usable rather than for the page.
    const box = await screen.findByPlaceholderText(SAVED_WEB_URL);
    await fill(person, box, "https://plex.example.net");

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
    await fill(person, box, "https://plex.example.net");

    await person.click(screen.getByRole("button", { name: "Security" }));
    await person.click(screen.getByRole("button", { name: "Discard and switch" }));

    expect(await screen.findByRole("heading", { name: "Security" })).toBeInTheDocument();
    expect(switchNotice()).toBeNull();
    expect(apiMock.setPlexSettings).not.toHaveBeenCalled();
  });

  it("switches straight through when the Plex web address still matches the stored one", async () => {
    stubMatchMedia(false);
    const person = renderSettings("plex");
    const box = await screen.findByPlaceholderText(SAVED_WEB_URL);
    // Filled and put back: the box diverged and returned, so there is nothing left to lose.
    await fill(person, box, SAVED_WEB_URL);

    await person.click(screen.getByRole("button", { name: "Security" }));

    expect(switchNotice()).toBeNull();
    expect(await screen.findByRole("heading", { name: "Security" })).toBeInTheDocument();
  });
});

describe("leaving Security or Backup with something unsaved", () => {
  // Security and Backup's drafts live in a CHILD component (`AdminPasswordForm`,
  // `RestoreCard`), so the unsaved-edits signal is declared there and reported up through the
  // panel. Driven through the real shell, since what this guard depends on is the wiring
  // between the child and the panel.
  const switchNotice = () =>
    [...document.querySelectorAll(".notice-warn")].find((n) =>
      n.textContent?.includes("Switching to"),
    ) ?? null;
  const heading = () => screen.getByRole("heading", { level: 2 }).textContent;

  /** Stage a backup file. The real input is `hidden` (a styled dropzone drives it), so this fires
   *  the change the file picker would, and waits for the password box the summary brings with it. */
  async function stageABackup() {
    const input = await waitFor(() => {
      const el = document.querySelector('input[type="file"]');
      if (!el) throw new Error("the backup panel has not loaded yet");
      return el;
    });
    fireEvent.change(input, { target: { files: [new File(["x"], "a.reaper")] } });
    return screen.findByLabelText(/admin password/i);
  }

  it("holds the switch on a typed admin password", async () => {
    stubMatchMedia(false);
    const person = renderSettings("security");
    const next = await screen.findByLabelText(/^new password$/i);
    await fill(person, next, "a-long-enough-password");

    await person.click(screen.getByRole("button", { name: "About" }));

    expect(switchNotice()!.textContent).toContain(
      "You have unsaved Security settings. Switching to About discards them.",
    );
    expect(heading()).toBe("Security");
    expect(next).toHaveValue("a-long-enough-password");
  });

  it("holds it for a password too short to save, and lets it go once the box is empty", async () => {
    // Both halves in one, because they are the same signal read twice: the report is "there is
    // something to lose", never "there is something Save would accept".
    stubMatchMedia(false);
    const person = renderSettings("security");
    const next = await screen.findByLabelText(/^new password$/i);
    await person.type(next, "short");

    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    await person.click(screen.getByRole("button", { name: "About" }));
    expect(switchNotice()).not.toBeNull();
    expect(heading()).toBe("Security");

    await person.click(screen.getByRole("button", { name: "Keep editing" }));
    await person.clear(next);
    await person.click(screen.getByRole("button", { name: "About" }));

    expect(switchNotice()).toBeNull();
    expect(await screen.findByRole("heading", { name: "About" })).toBeInTheDocument();
  });

  it("says what leaving Backup costs in Backup's own terms", async () => {
    // The shared sentence would be false here twice over: what is waiting is an uploaded file
    // rather than a setting, and leaving does not merely forget it. The card cancels the staged
    // upload on its way out, which the next test checks.
    stubMatchMedia(false);
    const person = renderSettings("backup");
    await stageABackup();

    await person.click(screen.getByRole("button", { name: "About" }));

    expect(switchNotice()!.textContent).toContain(
      "The backup file you chose isn't restored yet. Switching to About drops it.",
    );
    expect(heading()).toBe("Backup & Restore");
  });

  it("holds the switch while the upload is still going", async () => {
    // This is the window between dropping the file and the summary arriving. Checking the
    // summary alone would say there is nothing to lose here, which is wrong: the archive is
    // already on its way to the server, and once the card is gone an un-armed stage has no
    // surface anywhere in the app to reach it from.
    stubMatchMedia(false);
    // Left pending for the whole test, so the moment being asserted is the one the operator is
    // actually in: the file is uploading and no summary exists yet. Resolving it afterwards
    // would settle state after the last act(), which fails the run.
    apiMock.restorePrepare.mockReturnValue(new Promise(() => {}));
    const person = renderSettings("backup");
    const input = await waitFor(() => {
      const el = document.querySelector('input[type="file"]');
      if (!el) throw new Error("the backup panel has not loaded yet");
      return el;
    });
    fireEvent.change(input, { target: { files: [new File(["x"], "a.reaper")] } });

    await person.click(screen.getByRole("button", { name: "About" }));

    expect(switchNotice()).not.toBeNull();
    expect(heading()).toBe("Backup & Restore");
  });

  it("cancels the staged upload when Discard and switch is pressed", async () => {
    // Losing the file from the screen is only half of it: the archive is already on the SERVER,
    // and an un-armed stage has no surface anywhere in the app to clear it from later.
    stubMatchMedia(false);
    const person = renderSettings("backup");
    await stageABackup();

    await person.click(screen.getByRole("button", { name: "About" }));
    await person.click(screen.getByRole("button", { name: "Discard and switch" }));

    expect(await screen.findByRole("heading", { name: "About" })).toBeInTheDocument();
    await waitFor(() => expect(apiMock.restoreCancel).toHaveBeenCalledTimes(1));
    expect(apiMock.restoreConfirm).not.toHaveBeenCalled();
  });

  it("switches straight through from Backup with nothing staged, and cancels nothing", async () => {
    stubMatchMedia(false);
    const person = renderSettings("backup");
    await waitFor(() => expect(document.querySelector(".dropzone")).not.toBeNull());

    await person.click(screen.getByRole("button", { name: "About" }));

    expect(switchNotice()).toBeNull();
    expect(await screen.findByRole("heading", { name: "About" })).toBeInTheDocument();
    expect(apiMock.restoreCancel).not.toHaveBeenCalled();
  });

  it("switches straight through past an ARMED restore, and leaves it armed", async () => {
    // An armed restore is server state that outlives this card, this browser and this section,
    // so there is nothing here to lose and the guard must not fire. The card in that branch
    // carries its own Cancel button. Holding the switch would demand a discard for a decision
    // already stored, and sending the cancel would undo it.
    apiMock.backupInfo.mockResolvedValue({
      reaper_db_bytes: 1024,
      last_backup_at: null,
      key_in_backup: true,
      app_version: "test",
      restore_armed: true,
    });
    stubMatchMedia(false);
    const person = renderSettings("backup");
    expect(await screen.findByText(/A restore is ready/)).toBeInTheDocument();

    await person.click(screen.getByRole("button", { name: "About" }));

    expect(switchNotice()).toBeNull();
    expect(await screen.findByRole("heading", { name: "About" })).toBeInTheDocument();
    expect(apiMock.restoreCancel).not.toHaveBeenCalled();
  });
});
