// SPDX-License-Identifier: AGPL-3.0-or-later
// The restore card takes the admin password, so it holds one only while it is being used.
// The card is local to Settings.tsx, so these drive it the way an operator reaches it: the
// Backup panel, a staged file, then the password box that appears with the summary.
import { QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { testQueryClient } from "../test/queryClient";
import { Settings } from "./Settings";

const { apiMock } = vi.hoisted(() => ({
  apiMock: {
    backupInfo: vi.fn(),
    restorePrepare: vi.fn(),
    restoreConfirm: vi.fn(),
    // Sent by the card's own unmount, not by anything an operator clicks, so nothing in this file
    // names it and it was still missing when the card started sending it (rule 135).
    restoreCancel: vi.fn(),
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
  const queryClient = testQueryClient();
  const { unmount } = render(
    <QueryClientProvider client={queryClient}>
      <Settings initialPanel="backup" />
    </QueryClientProvider>,
  );
  return { person: userEvent.setup(), unmount, queryClient };
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
  apiMock.restoreCancel.mockResolvedValue({ ok: true });
});

describe("the admin password on the restore card", () => {
  it("is gone after a confirm that failed", async () => {
    // S-5. A wrong password is the likeliest reason to land here, and leaving it in the box
    // to be retried is how it stays in state; the field clears the way a sign-in form does.
    apiMock.restoreConfirm.mockRejectedValue(new Error("That password didn't match."));
    const { person } = renderBackupPanel();
    const password = await stage("a.reaper");
    await person.type(password, "a-password");

    await person.click(screen.getByRole("button", { name: /^restore$/i }));

    expect(await screen.findByText(/didn't match/)).toBeInTheDocument();
    expect(screen.getByLabelText(/admin password/i)).toHaveValue("");
  });

  it("does not carry over to the next file staged", async () => {
    // The password belongs to the summary it was typed against: staging another backup drops
    // the summary, which unmounts the box, and used to refill it against a different file.
    const { person } = renderBackupPanel();
    await person.type(await stage("a.reaper"), "a-password");

    await stage("b.reaper");

    expect(await screen.findByLabelText(/admin password/i)).toHaveValue("");
  });
});

describe("a staged backup nobody confirmed", () => {
  // `prepare` uploads and stages the archive on the SERVER. An un-armed stage has no surface
  // anywhere in the app -- the card only offers a Cancel once a restore is armed -- so a card
  // that goes away without saying anything used to leave that archive sitting there until the
  // next prepare replaced it.
  it("is canceled when the card goes, so nothing is left on the server", async () => {
    const { person, unmount } = renderBackupPanel();
    await person.type(await stage("a.reaper"), "a-password");

    unmount();

    expect(apiMock.restoreCancel).toHaveBeenCalledTimes(1);
  });

  it("sends no cancel when nothing was staged", async () => {
    const { unmount } = renderBackupPanel();
    await waitFor(() => expect(document.querySelector(".dropzone")).not.toBeNull());

    unmount();

    expect(apiMock.restoreCancel).not.toHaveBeenCalled();
  });

  it("leaves an ARMED restore alone, because cancel discards that one too", async () => {
    // The destructive direction, and the reason the cleanup is guarded on `staged` rather than on
    // the summary alone: `/restore/cancel` discards a staged OR ARMED restore, so a cleanup that
    // only asked "is a summary in state?" would quietly undo a restore already confirmed with the
    // admin password, and the operator would find out on the restart that changed nothing.
    //
    // The state that discriminates has to hold BOTH at once, which needs the arming to come from
    // outside this card: staged here, armed elsewhere -- a second tab, or the same operator's
    // phone -- and picked up by the refetch this panel does on focus and after a download. Rule
    // 118: an armed card with no local summary cannot tell the guarded cleanup from the unguarded
    // one, and passes for both.
    const { unmount, person, queryClient } = renderBackupPanel();
    await person.type(await stage("a.reaper"), "a-password");

    apiMock.backupInfo.mockResolvedValue({
      reaper_db_bytes: 1024,
      last_backup_at: null,
      key_in_backup: true,
      app_version: "test",
      restore_armed: true,
    });
    await act(() => queryClient.invalidateQueries({ queryKey: ["backup-info"] }));
    expect(await screen.findByText(/A restore is ready/)).toBeInTheDocument();

    unmount();

    expect(apiMock.restoreCancel).not.toHaveBeenCalled();
  });
});
