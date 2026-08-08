// SPDX-License-Identifier: AGPL-3.0-or-later
//
// What the wizard's last step is willing to call finished (#383).
//
// An install with Tautulli and one *arr and no Plex used to reach this panel, be told it was
// all set, and have its first real reap refused at the button four screens later. The refusal
// is right: the check for who is watching runs through Plex, and refusing is the keep
// direction. What was missing is that nothing said so until the operator had already picked
// what to delete.
//
// Driven against the step rather than through `SetupWizard`, because the wizard opens on the
// first *unfinished* step and an unlinked Plex is unfinished -- so a fresh mount lands on the
// Plex step, and this panel is reached by skipping forward within a session. The state is real;
// the route to it is navigation the wizard already owns and its own file already pins.
//
// Both directions are driven: a claim narrowed to nowhere is the same failure as a claim made
// everywhere, and only the pair can tell them apart.
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { SetupStatus } from "../api";
import { Announcer } from "../announce";
import { expectNoA11yViolations } from "../test/a11y";
import { IDLE_SCAN } from "../test/apiFixtures";
import { renderWithProviders } from "../test/renderWithProviders";
import { SetupScanStep } from "./SetupScanStep";

const { apiMock } = vi.hoisted(() => ({
  apiMock: {
    scanStatus: vi.fn(),
    startScan: vi.fn(),
    // The Discord row on this step reads it. Named though nothing below asserts on it, because
    // a mock that omits a read the tree performs renders its failed-read branch (rule 135).
    notifications: vi.fn(),
    // The pre-scan branch states the safety regime through the shared `SafetyBanner`, which
    // reads this. Only that branch does, which is why it arrived with the cases below.
    safety: vi.fn(),
  },
}));
vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  api: apiMock,
}));

/** Scanned, and a real run would go ahead. */
const DONE: SetupStatus = {
  admin_exists: true,
  has_password: true,
  plex_linked: true,
  instances: { radarr: 1, tautulli: 1 },
  has_radarr: true,
  has_sonarr: false,
  has_tautulli: true,
  has_seerr: false,
  has_scanned: true,
  scan_ready: true,
  reap_ready: true,
  complete: true,
};

/** The same install with Plex skipped: it scans, and it cannot reap. */
const DONE_NO_PLEX: SetupStatus = { ...DONE, plex_linked: false, reap_ready: false };

function renderStep(setup: SetupStatus, onGoToPlex = vi.fn(), onBack = vi.fn()) {
  renderWithProviders(
    <>
      {/* The app mounts this above every route, and `announce()` returns early with no region
          listening, so a test about what is spoken would pass against silence. */}
      <Announcer />
      {/* The landmark belongs to `SetupWizard`, which is what mounts every step, so the step
          is put back inside one rather than audited on a page with none. That the wizard
          really draws it is `SetupWizard.test.tsx`'s to prove, and it does. */}
      <main className="setup">
        <SetupScanStep setup={setup} onBack={onBack} onGoToPlex={onGoToPlex} onDone={vi.fn()} />
      </main>
    </>,
  );
  return { person: userEvent.setup(), onGoToPlex, onBack };
}

beforeEach(() => {
  vi.clearAllMocks();
  apiMock.scanStatus.mockResolvedValue(IDLE_SCAN);
  apiMock.notifications.mockResolvedValue({ has_webhook: false });
  apiMock.safety.mockResolvedValue({
    destructive_enabled: false,
    has_password: true,
    note: null,
  });
});

describe("the finish panel", () => {
  it("says you're all set when a real run could actually go ahead", async () => {
    renderStep(DONE);
    expect(await screen.findByText(/you're all set/i)).toBeInTheDocument();
    expect(screen.queryByText(/can't remove anything/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /connect plex/i })).not.toBeInTheDocument();
  });

  it("does not, when the first reap would be refused for want of Plex", async () => {
    renderStep(DONE_NO_PLEX);

    expect(await screen.findByText(/your library is scanned/i)).toBeInTheDocument();
    expect(screen.queryByText(/you're all set/i)).not.toBeInTheDocument();
    // In the words the Plex step and the Reap page use, off the one shared list, so the
    // constraint reads the same wherever the operator meets it. Found by its text rather than
    // by role: the notice is `standing`, so it shares `role="status"` with the app's own live
    // region, which the harness mounts beside it.
    const notice = screen.getByText(/until Plex is connected/i);
    expect(notice).toHaveClass("notice-warn");
    expect(notice).toHaveTextContent(/nobody is watching/i);
  });

  it("offers the way back to Plex inside the reason, not as a third button in the row", async () => {
    // Rule 42: a warning naming a fix carries the control that applies it. The placement is
    // pinned, not just the existence: the action row is navigation (Back, and the way
    // onward), so a repair affordance parked in it reads as a third way out of the step --
    // and it is the same shape the Reap page uses for the same job.
    const { person, onGoToPlex } = renderStep(DONE_NO_PLEX);
    const fix = await screen.findByRole("button", { name: /connect plex/i });
    expect(fix.closest(".notice")).not.toBeNull();
    expect(fix.closest(".step-actions")).toBeNull();

    await person.click(fix);
    expect(onGoToPlex).toHaveBeenCalled();
  });

  it("has no accessibility violations in that state", async () => {
    renderStep(DONE_NO_PLEX);
    await screen.findByText(/your library is scanned/i);
    // This step replaces the whole app shell, like every other, so it answers for its own
    // landmarks; `pageLevel` for the same reason the wizard's own audits use it.
    await expectNoA11yViolations(document.body, { pageLevel: true });
  });
});

describe("before anything is connected", () => {
  /** Connect skipped with nothing wired: the step is reachable, the scan is not. */
  const NOTHING: SetupStatus = {
    ...DONE,
    instances: {},
    has_radarr: false,
    has_tautulli: false,
    has_scanned: false,
    scan_ready: false,
    reap_ready: false,
    complete: false,
  };

  // "Skip for now" on the Connect step reaches here with nothing connected, and `POST
  // /api/scan/start` answers 200 whatever is wired: the refusal is raised inside the detached
  // task, so the panel had already turned into a spinner before the operator learned the scan
  // could never run. The message it lands on sends them to Settings, which is behind the wizard
  // they have not left; the step that fixes it is one press back.
  it("does not offer a scan the server must refuse", async () => {
    renderStep(NOTHING);

    const button = await screen.findByRole("button", { name: /run first scan/i });
    expect(button).toBeDisabled();
    expect(screen.getByText(/connect tautulli and one of radarr or sonarr/i)).toBeInTheDocument();
    expect(apiMock.startScan).not.toHaveBeenCalled();
  });

  it("carries the way back to the step that fixes it", async () => {
    const { person, onBack } = renderStep(NOTHING);

    await person.click(await screen.findByRole("button", { name: /connect services/i }));
    expect(onBack).toHaveBeenCalled();
  });

  it("still offers the scan once the services are there", async () => {
    renderStep({
      ...NOTHING,
      instances: { radarr: 1, tautulli: 1 },
      has_radarr: true,
      has_tautulli: true,
      scan_ready: true,
    });

    expect(await screen.findByRole("button", { name: /run first scan/i })).toBeEnabled();
    expect(
      screen.queryByText(/connect tautulli and one of radarr or sonarr/i),
    ).not.toBeInTheDocument();
  });
});
