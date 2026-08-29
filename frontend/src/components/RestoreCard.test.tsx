// SPDX-License-Identifier: AGPL-3.0-or-later
// The restore card takes the admin password, so it holds one only while it is being used.
// The card is local to BackupPanel.tsx, so these tests drive it the way an operator reaches
// it, through the Backup panel, a staged file, then the password box that appears with the
// summary.
import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { expectNoA11yViolations } from "../test/a11y";
import { testQueryClient } from "../test/queryClient";
import { renderWithProviders } from "../test/renderWithProviders";
import { Announcer } from "../announce";
import { RestoreFlow } from "./RestoreCard";
import { Settings } from "./Settings";

const { apiMock } = await vi.hoisted(async () => ({
  apiMock: (await import("../test/apiMock")).makeApiMock(),
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
  verdict: "current",
  key_in_backup: true,
  reaper_db_bytes: 1024,
  token: "staged-token",
};

function renderBackupPanel() {
  const queryClient = testQueryClient();
  const { unmount } = renderWithProviders(
    <>
      {/* The app mounts this above every route (`App.tsx`), and `announce()` returns early
          when no region is listening. Without it here, the card's sentences are dropped and a
          test about them would pass against silence. */}
      <Announcer />
      {/* `App` owns which panel is open, so the address bar can name it (navUrl.ts).
          Nothing here switches panel, so the owner does nothing. */}
      <Settings panel="backup" onPanelChange={() => {}} />
    </>,
    { client: queryClient },
  );
  return { person: userEvent.setup(), unmount, queryClient };
}

/** One bare `RestoreFlow` in its own tree and its own query cache, which is what two tabs are.
 *  The panel above cannot stand in for that. `Settings` mounts one card, and the state this
 *  tests needs two live at once, each holding the summary it staged. */
function renderFlow() {
  const { container, unmount } = renderWithProviders(
    <>
      <Announcer />
      <RestoreFlow armed={false} />
    </>,
  );
  return { container, unmount };
}

/** Stage a backup file. The real input is `hidden` (a styled dropzone drives it), so this
 *  fires the change the file picker would. */
async function stage(name: string) {
  return stageIn(document.body, name);
}

/** The same, scoped to one tree, for the tests that render more than one card. */
async function stageIn(root: HTMLElement, name: string) {
  const input = await waitFor(() => {
    const el = root.querySelector('input[type="file"]');
    if (!el) throw new Error("the backup panel has not loaded yet");
    return el;
  });
  fireEvent.change(input, { target: { files: [new File(["x"], name)] } });
  return within(root).findByLabelText(/admin password/i);
}

/** What the live region is holding, for the sentences this card speaks. */
const spoken = () =>
  [...document.querySelectorAll('[aria-live="polite"]')].map((n) => n.textContent).join("");

/** Stages a file, types the password, and confirms, reaching the card's armed branch the way
 *  an operator reaches it. Shared by the two describes that need it, rather than transcribed
 *  into both. */
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

beforeEach(() => {
  vi.clearAllMocks();
  apiMock.backupInfo.mockResolvedValue(INFO);
  apiMock.restorePrepare.mockResolvedValue(SUMMARY);
  apiMock.restoreCancel.mockResolvedValue({ ok: true });
  apiMock.restoreRestart.mockResolvedValue({ ok: true });
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
    // A wrong password is the likeliest reason to land here. The field clears the way a
    // sign-in form does, instead of leaving a rejected password sitting in the box to retry.
    apiMock.restoreConfirm.mockRejectedValue(new Error("That password didn't match."));
    const { person } = renderBackupPanel();
    const password = await stage("a.reaper");
    await person.type(password, "a-password");

    await person.click(screen.getByRole("button", { name: /^restore$/i }));

    expect(await screen.findByText(/didn't match/)).toBeInTheDocument();
    expect(screen.getByLabelText(/admin password/i)).toHaveValue("");
  });

  it("does not carry over to the next file staged", async () => {
    // The password belongs to the summary it was typed against. Staging another backup drops
    // the summary, which unmounts the password box, so it never carries over to a different
    // file.
    const { person } = renderBackupPanel();
    await person.type(await stage("a.reaper"), "a-password");

    await stage("b.reaper");

    expect(await screen.findByLabelText(/admin password/i)).toHaveValue("");
  });
});

describe("a staged backup nobody confirmed", () => {
  // `prepare` uploads and stages the archive on the server. An un-armed stage has no surface
  // anywhere in the app, since the card only offers a Cancel once a restore is armed. So a card
  // that goes away without cleaning up would leave that archive sitting there until the next
  // prepare replaced it.
  it("is canceled when the card goes, so nothing is left on the server", async () => {
    // This awaits the cancel instead of asserting right after `unmount()`. The cleanup asks
    // the server whether the restore was armed elsewhere before it sends anything (see the
    // armed case below), so the cancel is one round trip behind the unmount, not synchronous
    // with it.
    const { person, unmount } = renderBackupPanel();
    await person.type(await stage("a.reaper"), "a-password");

    unmount();

    await waitFor(() => expect(apiMock.restoreCancel).toHaveBeenCalledTimes(1));
    // The cancel is named, never bare. A tokenless cancel discards whatever is staged, and
    // what is staged is not always what this card put there (see the two-card case below).
    expect(apiMock.restoreCancel).toHaveBeenCalledWith(SUMMARY.token);
  });

  it("reclaims the archive it staged, never one a second card staged since", async () => {
    // `RestoreFlow` is live in two places now, Settings and the wizard's restore door, so an
    // operator can hold one in each tab. `stage_upload` replaces the staging directory rather
    // than adding to it. Tab 1 stages, tab 2 stages over it, and tab 1 can leave at any later
    // moment. An unscoped reclaim there would discard the second tab's archive, leaving tab 2
    // looking at a validated summary with nothing behind it.
    //
    // This pins the token the card sends. That the server then declines a stale one is proven
    // by `test_cancel_scoped_to_a_replaced_staging_leaves_it` in `tests/test_restore.py`.
    // Neither test alone proves the pair works together. The ownership check they share is
    // what does.
    const first = renderFlow();
    const second = renderFlow();
    apiMock.restorePrepare.mockResolvedValueOnce({ ...SUMMARY, token: "first-token" });
    await stageIn(first.container, "a.reaper");
    apiMock.restorePrepare.mockResolvedValueOnce({ ...SUMMARY, token: "second-token" });
    await stageIn(second.container, "b.reaper");

    first.unmount();

    await waitFor(() => expect(apiMock.restoreCancel).toHaveBeenCalledTimes(1));
    expect(apiMock.restoreCancel).toHaveBeenCalledWith("first-token");
    second.unmount();
  });

  it("scopes the operator's own Remove to the file named on the card", async () => {
    // `Remove` reaches the same discard from the same state, holding the same summary, so it
    // would strand the other card exactly as the unmount does. The armed Cancel below is the
    // one that stays unscoped, since it has no summary to take a token from.
    const { person } = renderBackupPanel();
    await stage("a.reaper");
    const remove = await screen.findByRole("button", { name: /^remove$/i });
    await waitFor(() => expect(remove).toBeEnabled());

    await person.click(remove);

    await waitFor(() => expect(apiMock.restoreCancel).toHaveBeenCalledWith(SUMMARY.token));
  });

  it("sends no cancel when nothing was staged", async () => {
    const { unmount } = renderBackupPanel();
    await waitFor(() => expect(document.querySelector(".dropzone")).not.toBeNull());

    unmount();

    expect(apiMock.restoreCancel).not.toHaveBeenCalled();
  });

  it("leaves an ARMED restore alone, because cancel discards that one too", async () => {
    // `/restore/cancel` discards a staged OR armed restore, so a cleanup that got this wrong
    // would quietly undo a restore already confirmed with the admin password. The operator
    // would only find out on the restart that changed nothing.
    //
    // The state that discriminates this has to hold both at once, so the arming has to come
    // from outside this card: staged here, armed elsewhere, such as a second tab or the same
    // operator's phone. Nothing refreshes ["backup-info"] from another client (the query sets
    // no `refetchInterval`, and `main.tsx` turns `refetchOnWindowFocus` off app-wide), so this
    // card is still rendering `armed: false` when it goes. This test does not invalidate that
    // query, deliberately: reading the stale cache is exactly the bug being guarded against,
    // and a test that hands the card the fresh answer could not catch it.
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
    // Leaving mid-confirm needs no discard click at all. `App` mounts Settings only while the
    // settings view is open, so one press on the top nav unmounts the card. The confirm is a
    // deliberately slow password verify behind a concurrency gate, so this window is real, and
    // the card is still holding its summary for every millisecond of it. This is the one send
    // this cleanup must never make, because the operator already paid the admin password for
    // it.
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
    // `prepare` stages on the server only when it resolves, which can be after the card is
    // gone. During that window the summary alone reads "nothing staged," so an archive that
    // lands after the card unmounts has nothing left to reclaim it and no surface in the app
    // to reach it.
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
    // The ordering is the assertion here, not just the eventual count. A cancel sent while the
    // upload is still in flight arrives before there is anything on the server to reclaim, so
    // the archive lands afterward and stays. Asserting only "a cancel was sent" would pass
    // either way.
    expect(apiMock.restoreCancel).not.toHaveBeenCalled();
    landed(SUMMARY);

    await waitFor(() => expect(apiMock.restoreCancel).toHaveBeenCalledTimes(1));
  });

  it("sends nothing when the confirm it waited on left no staging of ours to name", async () => {
    // This tests the `!token` guard. A confirm that succeeds drops the summary and its token
    // together, but `confirmRef` stays set until the refetch behind it settles, so an unmount
    // in that window walks past the early return with nothing of ours left to name. The refetch
    // is deliberately held open here: letting it settle would make the cleanup return one line
    // earlier, and this test could not fail whatever the guard did.
    //
    // The server is then made to answer "nothing armed," the only state where an unguarded
    // cleanup reaches its send. That send is the unscoped cancel, on a server whose staging may
    // by then belong to another card.
    apiMock.restoreConfirm.mockResolvedValue({ ok: true });
    let settle = (_: typeof INFO) => {};
    const { person, unmount } = renderBackupPanel();
    await person.type(await stage("a.reaper"), "a-password");
    apiMock.backupInfo.mockReturnValue(
      new Promise<typeof INFO>((resolve) => {
        settle = resolve;
      }),
    );

    await person.click(screen.getByRole("button", { name: /^restore$/i }));
    unmount();
    apiMock.backupInfo.mockResolvedValue({ ...INFO, restore_armed: false });
    settle({ ...INFO, restore_armed: false });

    await waitFor(() => expect(apiMock.restoreConfirm).toHaveBeenCalledTimes(1));
    expect(apiMock.restoreCancel).not.toHaveBeenCalled();
  });

  it("sends no cancel when the upload itself failed, because it staged nothing", async () => {
    // This is the other side of the window above. A prepare that rejects leaves no archive of
    // ours on the server, so a cancel here would be reclaiming somebody else's stage.
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
    // `Download backup` invalidates this very query, so a server that blinks lands here with
    // an archive staged and a password typed against it. The never-loaded sentence tells the
    // operator to reload, but a reload does not run the card's unmount cleanup, so following
    // that advice would leave the archive on the server with nothing able to reach it. This
    // line comes from one shared component, the same as the other three panels, so the
    // wording cannot drift between them.
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
    // The line that replaces it does not repeat the reload advice, since that would reopen the
    // same orphaned-archive exit described above.
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
  // A confirmed restore replaces the entire database on the next boot. Without a sentence
  // saying so, the only signal is the card becoming a different card, with the pressed Restore
  // button gone along with the form it lived on. Focus would fall to `<body>` and the next Tab
  // would restart above the whole page. This is the costliest silence on the Settings page, so
  // it has to speak.

  it("says a restore is ready, in the same words the card shows", async () => {
    // One pair of constants sits behind both, so the notice and the spoken sentence cannot be
    // reworded apart. This is asserted against the rendered notice, not against a copy of the
    // string, since that is the only version of this assertion that can catch them drifting
    // apart.
    await arm();

    // Scoped to the notice on the card, because the live region holds these same words too now,
    // which is exactly what this is meant to check. An unscoped text query cannot tell them
    // apart. Anchored on the bolded lead's parent, not on the first span in the card, since
    // `Notice` opens with its own visually-hidden "Warning: " label, which belongs to the
    // notice and not to this sentence.
    const notice = await waitFor(() => {
      const el = document.querySelector(".restore-armed strong")?.parentElement;
      if (!el) throw new Error("the card has not armed yet");
      return el;
    });
    expect(spoken()).toBe(notice.textContent);
    expect(spoken()).toContain("A restore is ready.");
  });

  it("leaves the operator on the armed card's reversible control, not the one that stops the app", async () => {
    // "Cancel restore" mounts before `busy` clears, so the check waits for it to become
    // actable, not just for it to exist. Focus lands on Cancel rather than on "Restart now,"
    // which is the continuation an operator wants but also the one that ends the process. A
    // programmatic focus move puts whatever it lands on under the next key pressed.
    await arm();

    const cancel = await screen.findByRole("button", { name: /cancel restore/i });
    await waitFor(() => expect(cancel).toBeEnabled());
    await waitFor(() => expect(cancel).toHaveFocus());
    expect(screen.getByRole("button", { name: /restart now/i })).not.toHaveFocus();
  });
});

describe("finishing the restore from the browser", () => {
  // A restore flow is otherwise entirely in the browser, so its last step has to be too. The
  // first-run wizard can open directly onto this flow, putting a brand-new operator in front
  // of it with no other tool at hand.

  it("stops Reaper, and claims only that", async () => {
    // The 200 response says the server accepted the stop. It does not say Reaper is back, and
    // this page cannot find out, since the connection it would ask over is the one going away.
    const { person } = await arm();
    const restart = await screen.findByRole("button", { name: /restart now/i });
    await waitFor(() => expect(restart).toBeEnabled());

    await person.click(restart);

    expect(await screen.findByText(/Reaper is stopping/)).toBeInTheDocument();
    expect(apiMock.restoreRestart).toHaveBeenCalledTimes(1);
    // This is waited for, not read straight off, since it is the second sentence this card
    // speaks, and `announce` drains its queue one `MESSAGE_GAP_MS` apart so neither is lost to
    // a batch.
    await waitFor(() => expect(spoken()).toContain("Reaper is stopping."));
    // Nothing anywhere on the card says Reaper came back, and the button that stops it is
    // gone, so a second press would be asking a server that is already going.
    expect(screen.queryByRole("button", { name: /restart now/i })).toBeNull();
    expect(screen.getByRole("button", { name: /reload/i })).toBeInTheDocument();
  });

  it("shows the server's refusal, on a card that had no failure surface at all", async () => {
    // The armed branch renders only a notice and a Cancel, so both buttons need their own way
    // to show a failure. Otherwise a failed action sets a sentence nothing draws, and the
    // operator only sees the button re-enable, with no sign the archive is still staged.
    apiMock.restoreRestart.mockRejectedValue(
      new Error("A reap is running. Let it finish or stop it, then restart Reaper."),
    );
    const { person } = await arm();
    const restart = await screen.findByRole("button", { name: /restart now/i });
    await waitFor(() => expect(restart).toBeEnabled());

    await person.click(restart);

    expect(await screen.findByText(/A reap is running/)).toBeInTheDocument();
    // Nothing was stopped, so the card stays exactly where it was and the button is pressable
    // again once the run ends.
    expect(screen.queryByText(/Reaper is stopping/)).toBeNull();
    await waitFor(() => expect(screen.getByRole("button", { name: /restart now/i })).toBeEnabled());
  });

  it("cancels unscoped, because an armed restore need not be the one this card staged", async () => {
    // This is why a token-scoped cancel is not used everywhere. `armed` is server state that
    // outlives this browser, so the card offering Cancel here may never have seen the summary
    // behind it. A restore armed from a phone shows on the desktop too, and a token-scoped
    // Cancel there would refuse the one press that clears it, leaving the operator with an
    // armed restore and no way out.
    const { person } = await arm();
    const cancel = await screen.findByRole("button", { name: /cancel restore/i });
    await waitFor(() => expect(cancel).toBeEnabled());

    await person.click(cancel);

    // The absence of a token is the assertion here, not the argument count. `restoreCancel()`
    // and `restoreCancel(undefined)` are one request on the wire, and pinning which spelling
    // reached the mock would fail on a refactor that changed nothing an operator can see.
    await waitFor(() => expect(apiMock.restoreCancel).toHaveBeenCalled());
    expect(apiMock.restoreCancel.mock.calls[0]?.[0]).toBeUndefined();
  });

  it("asks the server before an unscoped discard, so it cannot take a newer archive", async () => {
    // This is the one discard a token cannot scope. `armed` is a cached read nothing refreshes
    // from another client, so this card goes on drawing the armed branch after that restore was
    // canceled elsewhere and a fresh archive staged in its place. An unconditional discard then
    // destroys the new one. This is the same shape as the unmount reclaim's guard, and the same
    // fix: reading the stale cache is the risk, so the server is asked directly instead.
    //
    // This test does not invalidate that query, deliberately. Handing the card the fresh answer
    // would hide the defect, since the card would simply stop rendering the button under test.
    const { person } = await arm();
    const cancel = await screen.findByRole("button", { name: /cancel restore/i });
    await waitFor(() => expect(cancel).toBeEnabled());
    apiMock.backupInfo.mockResolvedValue({ ...INFO, restore_armed: false });

    await person.click(cancel);

    // The card showing what is true now is the evidence that the server was asked, since
    // nothing else in this branch can move it off the armed state. This is asserted on the
    // controls, rather than on the armed sentence, which the live region is still holding from
    // the confirm, and rather than on a call count, which the refetch behind the redraw makes
    // a moving number.
    expect(await screen.findByText(/Drop a backup file here/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /cancel restore/i })).toBeNull();
    expect(apiMock.restoreCancel).not.toHaveBeenCalled();
  });

  it("shows a failed Cancel too, which was the silent half of the same gap", async () => {
    apiMock.restoreCancel.mockRejectedValue(new Error("the disk is full"));
    const { person } = await arm();
    const cancel = await screen.findByRole("button", { name: /cancel restore/i });
    await waitFor(() => expect(cancel).toBeEnabled());

    await person.click(cancel);

    expect(await screen.findByText(/still waiting to be restored/)).toBeInTheDocument();
  });
});
