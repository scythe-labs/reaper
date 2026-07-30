// SPDX-License-Identifier: AGPL-3.0-or-later
// The restore card takes the admin password, so it holds one only while it is being used.
// The card is local to Settings.tsx, so these drive it the way an operator reaches it: the
// Backup panel, a staged file, then the password box that appears with the summary.
import { QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { expectNoA11yViolations } from "../test/a11y";
import { testQueryClient } from "../test/queryClient";
import { Announcer } from "../announce";
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

function renderBackupPanel() {
  const queryClient = testQueryClient();
  const { unmount } = render(
    <QueryClientProvider client={queryClient}>
      {/* The app mounts this above every route (`App.tsx`), and `announce()` returns early when no
          region is listening -- so without it here the card's sentences are dropped and a test
          about them passes against silence. */}
      <Announcer />
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
  apiMock.backupInfo.mockResolvedValue(INFO);
  apiMock.restorePrepare.mockResolvedValue(SUMMARY);
  apiMock.restoreCancel.mockResolvedValue({ ok: true });
});

describe("the admin password on the restore card", () => {
  // A restore replaces the whole database, and this card takes the admin password that authorizes
  // it. An operator who cannot find the password box has an archive already uploaded and no way
  // to finish or undo it.
  it("has no accessibility violations", async () => {
    renderBackupPanel();
    await stage("a.reaper");
    await expectNoA11yViolations();
  });

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
    // Awaited, not asserted straight after `unmount()`: the cleanup asks the server whether the
    // restore was armed elsewhere before it sends anything (see the armed case below), so the
    // cancel is one round trip behind the unmount rather than synchronous with it.
    const { person, unmount } = renderBackupPanel();
    await person.type(await stage("a.reaper"), "a-password");

    unmount();

    await waitFor(() => expect(apiMock.restoreCancel).toHaveBeenCalledTimes(1));
  });

  it("sends no cancel when nothing was staged", async () => {
    const { unmount } = renderBackupPanel();
    await waitFor(() => expect(document.querySelector(".dropzone")).not.toBeNull());

    unmount();

    expect(apiMock.restoreCancel).not.toHaveBeenCalled();
  });

  it("leaves an ARMED restore alone, because cancel discards that one too", async () => {
    // The destructive direction: `/restore/cancel` discards a staged OR ARMED restore, so a
    // cleanup that got this wrong would quietly undo a restore already confirmed with the admin
    // password, and the operator would find out on the restart that changed nothing.
    //
    // The state that discriminates has to hold BOTH at once, which needs the arming to come from
    // outside this card: staged here, armed elsewhere -- a second tab, or the same operator's
    // phone. Nothing refreshes ["backup-info"] from another client (the query sets no
    // `refetchInterval` and `main.tsx` turns `refetchOnWindowFocus` off app-wide), so this card is
    // still rendering `armed: false` when it goes. No invalidation here, deliberately: reading the
    // cache is the bug, and a test that hands the card the answer cannot see it.
    const { unmount, person } = renderBackupPanel();
    await person.type(await stage("a.reaper"), "a-password");
    expect(screen.queryByText(/A restore is ready/)).not.toBeInTheDocument();

    apiMock.backupInfo.mockResolvedValue({ ...INFO, restore_armed: true });
    const asked = apiMock.backupInfo.mock.calls.length;

    unmount();

    await waitFor(() => expect(apiMock.backupInfo).toHaveBeenCalledTimes(asked + 1));
    expect(apiMock.restoreCancel).not.toHaveBeenCalled();
  });

  it("sends no cancel while the confirm it was authorized with is still in flight", async () => {
    // Leaving mid-confirm needs no discard click at all: `App` mounts Settings only while the
    // settings view is open, so one press on the top nav unmounts the card. The confirm is a
    // deliberately slow password verify behind a concurrency gate, so the window is real, and the
    // card is still holding its summary for every millisecond of it -- the one send this cleanup
    // must never make, because the operator already paid the admin password for it.
    let confirmed = () => {};
    apiMock.restoreConfirm.mockReturnValue(
      new Promise<void>((resolve) => {
        confirmed = () => resolve();
      }),
    );
    const { person, unmount } = renderBackupPanel();
    await person.type(await stage("a.reaper"), "a-password");
    await person.click(screen.getByRole("button", { name: /^restore$/i }));
    const asked = apiMock.backupInfo.mock.calls.length;

    unmount();
    apiMock.backupInfo.mockResolvedValue({ ...INFO, restore_armed: true });
    confirmed();

    await waitFor(() => expect(apiMock.backupInfo).toHaveBeenCalledTimes(asked + 1));
    expect(apiMock.restoreCancel).not.toHaveBeenCalled();
  });

  it("cancels the archive an upload lands after the card is already gone", async () => {
    // `prepare` stages on the SERVER when it resolves, which can be after the card goes. Reading
    // the summary alone said "nothing staged" during that window, so the archive arrived with
    // nothing left to reclaim it and no surface in the app to reach it.
    let landed = (_: typeof SUMMARY) => {};
    apiMock.restorePrepare.mockReturnValue(
      new Promise<typeof SUMMARY>((resolve) => {
        landed = resolve;
      }),
    );
    const { unmount } = renderBackupPanel();
    const input = await waitFor(() => {
      const el = document.querySelector('input[type="file"]');
      if (!el) throw new Error("the backup panel has not loaded yet");
      return el;
    });
    fireEvent.change(input, { target: { files: [new File(["x"], "a.reaper")] } });

    unmount();
    // The ordering IS the assertion, not just the eventual count: a cancel sent while the upload
    // is still in flight arrives before there is anything on the server to reclaim, so the archive
    // lands afterwards and stays. Asserting only "a cancel was sent" passes for both.
    expect(apiMock.restoreCancel).not.toHaveBeenCalled();
    landed(SUMMARY);

    await waitFor(() => expect(apiMock.restoreCancel).toHaveBeenCalledTimes(1));
  });

  it("sends no cancel when the upload itself failed, because it staged nothing", async () => {
    // The other side of the window above: a prepare that rejected left no archive of ours on the
    // server, so a cancel here would be reclaiming somebody else's stage.
    let failed = (_: Error) => {};
    apiMock.restorePrepare.mockReturnValue(
      new Promise<typeof SUMMARY>((_resolve, reject) => {
        failed = reject;
      }),
    );
    const { unmount } = renderBackupPanel();
    const input = await waitFor(() => {
      const el = document.querySelector('input[type="file"]');
      if (!el) throw new Error("the backup panel has not loaded yet");
      return el;
    });
    fireEvent.change(input, { target: { files: [new File(["x"], "a.reaper")] } });

    unmount();
    failed(new Error("That file couldn't be read."));

    await waitFor(() => expect(apiMock.restorePrepare).toHaveBeenCalledTimes(1));
    expect(apiMock.restoreCancel).not.toHaveBeenCalled();
  });
});

describe("the backup panel's own read failing", () => {
  it("keeps the staged card and says the read is stale, not that the page never loaded", async () => {
    // `Download backup` invalidates this very query, so a server that blinks lands here with an
    // archive staged and a password typed against it. The never-loaded sentence tells the operator
    // to reload, and a reload does not run the card's unmount cleanup, so following the panel's own
    // advice was the one exit that left the archive on the server with nothing able to reach it.
    // Same line as the other three panels, from one component, so the wording cannot drift
    // (rules 72 and 144).
    const { person, queryClient } = renderBackupPanel();
    await person.type(await stage("a.reaper"), "a-password");

    apiMock.backupInfo.mockRejectedValue(new Error("boom"));
    const asked = apiMock.backupInfo.mock.calls.length;
    await act(() => queryClient.invalidateQueries({ queryKey: ["backup-info"] }));
    await waitFor(() => expect(apiMock.backupInfo.mock.calls.length).toBeGreaterThan(asked));

    expect(screen.queryByText(/Couldn't load this page/)).toBeNull();
    const stale = await screen.findByText(/Couldn't check these settings just now/);
    expect(stale).toHaveClass("notice-warn");
    expect(screen.getByLabelText(/admin password/i)).toHaveValue("a-password");
    // And the line that replaced it does not repeat the advice (#153). It said "Reload to try
    // again." above this staged archive and this typed password until then, which is the same
    // orphaned-archive exit the comment above describes, reintroduced by the fix for it.
    expect(stale).not.toHaveTextContent(/reload/i);
  });

  it("still says the page never loaded when the first read is the one that fails", async () => {
    apiMock.backupInfo.mockRejectedValue(new Error("boom"));
    renderBackupPanel();

    expect(await screen.findByText(/Couldn't load this page/)).toBeInTheDocument();
    expect(document.querySelector(".dropzone")).toBeNull();
  });
});

describe("what an operator is told when a restore arms", () => {
  // A confirmed restore replaces the entire database on the next boot, and its ONLY signal was the
  // card becoming a different card: no sentence, and the pressed Restore button gone with the form
  // it lived on, so focus fell to `<body>` and the next Tab restarted above the whole page. An
  // absence cannot be heard, and this is the costliest absence in Settings (#173).
  const spoken = () =>
    [...document.querySelectorAll('[aria-live="polite"]')].map((n) => n.textContent).join("");

  async function arm() {
    apiMock.restoreConfirm.mockResolvedValue({ ok: true });
    const { person, queryClient } = renderBackupPanel();
    const password = await stage("a.reaper");
    await person.type(password, "a-password");
    // The refetch after the confirm is what flips the card into its armed branch.
    apiMock.backupInfo.mockResolvedValue({ ...INFO, restore_armed: true });
    await person.click(screen.getByRole("button", { name: /^restore$/i }));
    return { person, queryClient };
  }

  it("says a restore is ready, in the same words the card shows", async () => {
    // One pair of constants behind both, so the notice and the sentence cannot be reworded apart
    // (rule 144). Asserted against the rendered notice rather than against a copy of the string,
    // which is the only version of this assertion that can catch them drifting.
    await arm();

    // Scoped to the notice on the card, because the live region holds these words too now -- which
    // is the whole point, and also why an unscoped text query cannot be the assertion.
    // Anchored on the bolded lead's parent, not on the first span in the card: `Notice` opens with
    // its own visually-hidden "Warning: ", which belongs to the notice and not to this sentence.
    const notice = await waitFor(() => {
      const el = document.querySelector(".restore-armed strong")?.parentElement;
      if (!el) throw new Error("the card has not armed yet");
      return el;
    });
    expect(spoken()).toBe(notice.textContent);
    expect(spoken()).toContain("A restore is ready.");
  });

  it("leaves the operator on the one control the armed card has", async () => {
    // "Cancel restore" is the only way back out, and it mounts a commit before `busy` clears, so
    // the move has to wait for it to become actable rather than for it to exist.
    await arm();

    const cancel = await screen.findByRole("button", { name: /cancel restore/i });
    await waitFor(() => expect(cancel).toBeEnabled());
    await waitFor(() => expect(cancel).toHaveFocus());
  });
});
