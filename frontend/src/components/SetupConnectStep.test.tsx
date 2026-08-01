// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The restore door on the wizard's Connect step (#385).
//
// Two claims, and the second is the one worth a file of its own. The door **exists on this
// step**, because an operator moving an existing Reaper here should meet it before doing the
// work it replaces, not after. And it **confirms with the password already typed**: the wizard
// holds the string from step one, so the same string reaches `restoreConfirm` without the
// operator being asked for it a second time, and the box is not merely hidden.
//
// The third case is the one an implementation is likeliest to skip: a wizard resumed in a fresh
// tab holds no password, and the flow has to fall back to the box rather than sending an empty
// string at a route that would refuse it.
import { QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { SetupStatus } from "../api";
import { Announcer } from "../announce";
import { expectNoA11yViolations } from "../test/a11y";
import { testQueryClient } from "../test/queryClient";
import { SetupConnectStep } from "./SetupConnectStep";

const { apiMock } = vi.hoisted(() => ({
  apiMock: {
    instances: vi.fn(),
    backupInfo: vi.fn(),
    restorePrepare: vi.fn(),
    restoreConfirm: vi.fn(),
    // Sent by the flow's own unmount rather than by anything an operator clicks, so nothing
    // below names it and it would still be missing without this line (rule 135).
    restoreCancel: vi.fn(),
  },
}));
vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  api: apiMock,
}));

const SCAN_READY: SetupStatus = {
  admin_exists: true,
  has_password: true,
  plex_linked: true,
  instances: { radarr: 1, tautulli: 1 },
  has_radarr: true,
  has_sonarr: false,
  has_tautulli: true,
  has_seerr: false,
  has_scanned: false,
  scan_ready: true,
  reap_ready: true,
  complete: false,
};

const INFO = {
  reaper_db_bytes: 1024,
  last_backup_at: null,
  key_in_backup: true,
  app_version: "test",
  restore_armed: false,
};

const SUMMARY = {
  app_version: null,
  created_at: null,
  revision: null,
  verdict: "current",
  key_in_backup: true,
  reaper_db_bytes: 1024,
  token: "staged-token",
};

/** The step as the wizard mounts it. `password` is what step one handed up: a string once it
 *  has been typed this session, null on a flow resumed in another tab. */
function renderStep(password: string | null) {
  render(
    <QueryClientProvider client={testQueryClient()}>
      {/* The app mounts this above every route, and `announce()` returns early with no region
          listening, so the armed sentence would be dropped without it. */}
      <Announcer />
      <SetupConnectStep setup={SCAN_READY} password={password} onBack={vi.fn()} onNext={vi.fn()} />
    </QueryClientProvider>,
  );
  return userEvent.setup();
}

/** Open the door, then stage a backup file. The real input is `hidden` behind a styled
 *  dropzone, so this fires the change the file picker would. */
async function openAndStage(person: ReturnType<typeof userEvent.setup>) {
  await person.click(await screen.findByRole("button", { name: /restore a backup/i }));
  const input = await waitFor(() => {
    const el = document.querySelector('input[type="file"]');
    if (!el) throw new Error("the restore flow has not loaded yet");
    return el;
  });
  fireEvent.change(input, { target: { files: [new File(["x"], "a.reaper")] } });
  // Wait for the staged summary, whose arrival is what puts Restore on screen at all. Waiting
  // for the control rather than for the page, because user-event reports a click on a control
  // that is not there yet as nothing at all (rule 137).
  return screen.findByRole("button", { name: /^restore$/i });
}

beforeEach(() => {
  vi.clearAllMocks();
  apiMock.instances.mockResolvedValue([]);
  apiMock.backupInfo.mockResolvedValue(INFO);
  apiMock.restorePrepare.mockResolvedValue(SUMMARY);
  apiMock.restoreConfirm.mockResolvedValue({ ok: true });
  apiMock.restoreCancel.mockResolvedValue({ ok: true });
});

describe("the restore door on the Connect step", () => {
  it("offers a restore beside the services it is the alternative to", async () => {
    renderStep("the-typed-password");
    expect(await screen.findByRole("button", { name: /restore a backup/i })).toBeInTheDocument();
  });

  it("opens the restore flow as a real dialog", async () => {
    const person = renderStep("the-typed-password");
    await person.click(await screen.findByRole("button", { name: /restore a backup/i }));
    // A dialog, not a step card: there IS a page behind this one and an opener to return focus
    // to, which is the whole of `ModalShell`'s contract.
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveAccessibleName(/restore from a backup/i);
  });

  it("has no accessibility violations with a backup staged", async () => {
    const person = renderStep("the-typed-password");
    await openAndStage(person);
    await expectNoA11yViolations();
  });

  it("confirms with the password from step one, without asking for it again", async () => {
    const person = renderStep("the-typed-password");
    const restore = await openAndStage(person);

    // No box, and Restore is actable without one being filled in -- the two halves of "not
    // asked again". A hidden box the operator still had to satisfy would pass the first
    // assertion and fail the operator.
    expect(screen.queryByLabelText(/admin password/i)).not.toBeInTheDocument();
    expect(restore).toBeEnabled();
    await person.click(restore);

    // The real point: the held string reaches the route, so the backend's own password check
    // is untouched and confirms against exactly what was set a minute ago.
    await waitFor(() =>
      expect(apiMock.restoreConfirm).toHaveBeenCalledWith("the-typed-password", "staged-token"),
    );
  });

  it("asks for the password when this browser never held one", async () => {
    // A wizard resumed in a fresh tab: the password step is behind the operator on the server,
    // so it is not shown again, and nothing in this browser holds what they typed.
    const person = renderStep(null);
    const restore = await openAndStage(person);

    const box = await screen.findByLabelText(/admin password/i);
    expect(restore).toBeDisabled();
    await person.type(box, "typed-here-instead");
    await person.click(restore);

    await waitFor(() =>
      expect(apiMock.restoreConfirm).toHaveBeenCalledWith("typed-here-instead", "staged-token"),
    );
  });

  it("gives the box back when the held password is refused", async () => {
    // It cannot normally be wrong. But the only correction available from inside this modal is
    // the box, so a refusal that left it hidden would leave the operator pressing Restore
    // against a string they can neither see nor change.
    apiMock.restoreConfirm.mockRejectedValue(new Error("That password didn't match."));
    const person = renderStep("no-longer-right");
    await person.click(await openAndStage(person));

    expect(await screen.findByText(/didn't match/)).toBeInTheDocument();
    expect(await screen.findByLabelText(/admin password/i)).toHaveValue("");
  });

  it("says a restore is already waiting, rather than inviting a second upload", async () => {
    // `restore_armed` is server state: it survives a reload and shows even if this browser
    // never did the confirm. Offering the dropzone over it would stage an archive on top of a
    // decision already made.
    apiMock.backupInfo.mockResolvedValue({ ...INFO, restore_armed: true });
    const person = renderStep("the-typed-password");
    await person.click(await screen.findByRole("button", { name: /restore a backup/i }));

    expect(await screen.findByText(/a restore is ready/i)).toBeInTheDocument();
    expect(document.querySelector('input[type="file"]')).toBeNull();
    expect(screen.getByRole("button", { name: /cancel restore/i })).toBeInTheDocument();
  });
});
