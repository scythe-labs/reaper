// SPDX-License-Identifier: AGPL-3.0-or-later
// The restore card takes the admin password, so it holds one only while it is being used.
// The card is local to Settings.tsx, so these drive it the way an operator reaches it: the
// Backup panel, a staged file, then the password box that appears with the summary.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { Settings } from "./Settings";

const { apiMock } = vi.hoisted(() => ({
  apiMock: {
    backupInfo: vi.fn(),
    restorePrepare: vi.fn(),
    restoreConfirm: vi.fn(),
    downloadBackup: vi.fn(),
  },
}));

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  api: apiMock,
}));

const SUMMARY = {
  app_version: null,
  created_at: null,
  revision: null,
  verdict: "current",
  key_in_backup: true,
  reaper_db_bytes: 1024,
  token: "staged-token",
};

function renderBackupPanel() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <Settings initialPanel="backup" />
    </QueryClientProvider>,
  );
  return userEvent.setup();
}

/** Stage a backup file. The real input is `hidden` (a styled dropzone drives it), so this
 *  fires the change the file picker would. */
async function stage(name: string) {
  const input = await waitFor(() => {
    const el = document.querySelector('input[type="file"]');
    if (!el) throw new Error("the backup panel has not loaded yet");
    return el;
  });
  fireEvent.change(input, { target: { files: [new File(["x"], name)] } });
  return screen.findByLabelText(/admin password/i);
}

beforeEach(() => {
  vi.clearAllMocks();
  apiMock.backupInfo.mockResolvedValue({
    reaper_db_bytes: 1024,
    last_backup_at: null,
    key_in_backup: true,
    app_version: "test",
    restore_armed: false,
  });
  apiMock.restorePrepare.mockResolvedValue(SUMMARY);
});

describe("the admin password on the restore card", () => {
  it("is gone after a confirm that failed", async () => {
    // S-5. A wrong password is the likeliest reason to land here, and leaving it in the box
    // to be retried is how it stays in state; the field clears the way a sign-in form does.
    apiMock.restoreConfirm.mockRejectedValue(new Error("That password didn't match."));
    const person = renderBackupPanel();
    const password = await stage("a.reaper");
    await person.type(password, "a-password");

    await person.click(screen.getByRole("button", { name: /^restore$/i }));

    expect(await screen.findByText(/didn't match/)).toBeInTheDocument();
    expect(screen.getByLabelText(/admin password/i)).toHaveValue("");
  });

  it("does not carry over to the next file staged", async () => {
    // The password belongs to the summary it was typed against: staging another backup drops
    // the summary, which unmounts the box, and used to refill it against a different file.
    const person = renderBackupPanel();
    await person.type(await stage("a.reaper"), "a-password");

    await stage("b.reaper");

    expect(await screen.findByLabelText(/admin password/i)).toHaveValue("");
  });
});
