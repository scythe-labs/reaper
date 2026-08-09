// SPDX-License-Identifier: AGPL-3.0-or-later
// The settings shell has two forms of the same navigation: a rail of ten tabs on a wide screen,
// one picker below NARROW_SCREEN_QUERY. jsdom has no `matchMedia`, so `useMediaQuery` reports
// false and every other suite in this tree exercises the rail; the picker is only reachable with
// the query stubbed, which is what these do. Only one of the two is ever rendered, so each test
// also asserts the absence of the other -- a CSS-hidden twin would leave both in the tree.
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
import { PANELS as DECLARED_PANELS, Settings } from "./Settings";

const { apiMock } = await vi.hoisted(async () => ({
  apiMock: (await import("../test/apiMock")).makeApiMock(),
}));

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  api: apiMock,
}));

// The spec, written out rather than derived: these are the words an operator reads, so a rename
// has to be typed here too. The test below reconciles the table with the declaration it mirrors --
// deriving it instead would assert the rail against itself (rule 119).
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

// Rule 133: the stub is process-global, so it never outlives its own test.
afterEach(() => {
  vi.unstubAllGlobals();
});

function renderSettings(
  initialPanel: "about" | "general" | "plex" | "notifications" | "security" | "backup" = "about",
) {
  // Seeded, not just mocked: the General panel renders its fields from this read, and a mocked
  // answer lands a microtask later -- after a synchronous assertion, which would then be about
  // the "Loading…" panel rather than the one an operator types into (rule 136).
  renderWithProviders(<Settings initialPanel={initialPanel} />, {
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
    // One set, two hand copies: this table and `PANELS` in Settings.tsx. A new section used to
    // fail only here, on the labels, which reads as a rail bug -- while the thing that actually
    // needed doing was classifying the new section in the switch guard, and the suite went green
    // again the moment a label was appended (#156). `dirtyPanels` is a total record now, so the
    // compiler owns that half; this owns the label half and names the other one (rules 103, 144).
    expect(
      DECLARED_PANELS.map((p) => p.label),
      "Settings.tsx's PANELS changed. Update this table, and classify the section in " +
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
    // One edit, not twenty-six keystrokes: what this family needs is a draft in the box, and
    // paying a panel re-render per character is what put these six tests within a few hundred
    // milliseconds of the 5000ms timeout on a loaded runner (`src/test/forms.ts`).
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
  // Scoped to the notice that names a switch, because the Plex panel raises a `.notice-warn` of
  // its own against this fixture: `plexResources` answers with an empty server list while the
  // status says linked, so the linked server reads as missing. A bare `.notice-warn` lookup would
  // pass on that one. Name the notice this fixture really renders, not a plausible one -- the
  // certificate warning is absent here, since `plexStatus` returns `verify_tls: true`.
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
    // the way an operator sees it. Rule 137: the panel early-returns "Loading…" until the status
    // read lands, so wait for the box to be usable rather than for the page.
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
  // The last two panels the guard did not reach, and the two that needed a hop the other three
  // did not: their drafts live in a CHILD component (`AdminPasswordForm`, `RestoreCard`), so the
  // signal is declared there and reported up through the panel. Driven through the real shell,
  // because what was broken is the wiring between them.
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
    // rather than a setting, and leaving does not merely forget it -- the card cancels the staged
    // upload on its way out, which is the next test.
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
    // The window between dropping the file and the summary arriving, which reading the summary
    // alone reported as nothing to lose. It is the costliest exit of the lot: the archive is
    // already on its way, so it lands after the card is gone, and an un-armed stage has no surface
    // anywhere in the app to reach it from.
    stubMatchMedia(false);
    // Left pending for the whole test, so the moment being asserted is the one the operator is
    // actually in: the file is uploading and no summary exists yet. Resolving it afterwards would
    // settle state after the last act() and is what rule 136 fails the run over.
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
    // Rule 146: an armed restore is server state that outlives this card, this browser and this
    // section, so there is nothing here to lose and the guard must not fire. The card in that
    // branch carries its own Cancel; holding the switch would demand a discard for a decision
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
