// SPDX-License-Identifier: AGPL-3.0-or-later
// The restore card takes the admin password, so it holds one only while it is being used.
// The card is local to Settings.tsx, so these drive it the way an operator reaches it: the
// Backup panel, a staged file, then the password box that appears with the summary.
import { QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { expectNoA11yViolations } from "../test/a11y";
import { testQueryClient } from "../test/queryClient";
import { Announcer } from "../announce";
import { RestoreFlow } from "./RestoreCard";
import { Settings } from "./Settings";

const { apiMock } = vi.hoisted(() => ({
  apiMock: {
    backupInfo: vi.fn(),
    restorePrepare: vi.fn(),
    restoreConfirm: vi.fn(),
    // Sent by the card's own unmount, not by anything an operator clicks, so nothing in this file
    // names it and it was still missing when the card started sending it (rule 135).
    restoreCancel: vi.fn(),
    // Pressed only from the armed card, which most of this file never reaches -- so it is
    // declared here rather than where a test happens to name it (rule 135).
    restoreRestart: vi.fn(),
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

/** One bare `RestoreFlow` in its own tree and its own query cache, which is what two tabs are.
 *  The panel above cannot stand in for that: `Settings` mounts one card, and the state #387
 *  turns on needs two live at once, each holding the summary it staged. */
function renderFlow() {
  const { container, unmount } = render(
    <QueryClientProvider client={testQueryClient()}>
      <Announcer />
      <RestoreFlow armed={false} />
    </QueryClientProvider>,
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

/** Stage, type the password, and confirm: the card's armed branch, reached the way an operator
 *  reaches it. Shared by the two describes that need it rather than transcribed into both. */
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
    // Named, never bare: a tokenless cancel discards whatever is staged, and what is staged is
    // not always what this card put there (#387, and the two-card case below).
    expect(apiMock.restoreCancel).toHaveBeenCalledWith(SUMMARY.token);
  });

  it("reclaims the archive it staged, never one a second card staged since", async () => {
    // #387, driven as the two cards it takes. `RestoreFlow` is live in two places now --
    // Settings, and the wizard's restore door -- so an operator can hold one in each tab, and
    // `stage_upload` REPLACES the staging directory rather than adding to it. Tab 1 stages,
    // tab 2 stages over it, tab 1 leaves at any later moment: no timing coincidence, and the
    // first card's reclaim used to discard the second's archive. Tab 2 was then looking at a
    // validated summary whose Restore had nothing behind it.
    //
    // What this half pins is the token the card SENDS. That the server then declines it is
    // `test_cancel_scoped_to_a_replaced_staging_leaves_it` in `tests/test_restore.py`; neither
    // test proves the pair alone, and the ownership check they share is the seam.
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
    // Rule 72: `Remove` reaches the same discard from the same state, holding the same summary,
    // so it strands the other card exactly as the unmount did. The armed Cancel is the one that
    // stays unscoped, below -- it has no summary to take a token from.
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

  it("sends nothing when the confirm it waited on left no staging of ours to name", async () => {
    // Rule 118, for the `!token` guard. A confirm that succeeds drops the summary and its token
    // together, but `confirmRef` stays set until the refetch behind it settles -- so an unmount
    // in that window walks past the early return with nothing of ours left to name. Held open
    // here deliberately: with the refetch allowed to settle, the cleanup returns one line
    // earlier and this test could not fail whatever the guard did.
    //
    // The server is then made to answer "nothing armed", which is the only state where an
    // unguarded cleanup reaches its send -- and that send is the unscoped cancel, on a server
    // whose staging may by then belong to another card.
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

  it("leaves the operator on the armed card's reversible control, not the one that stops the app", async () => {
    // "Cancel restore" mounts a commit before `busy` clears, so the move has to wait for it to
    // become actable rather than for it to exist. It takes the landing rather than "Restart now",
    // which is the continuation an operator wants but also the one that ends the process: a
    // programmatic focus move puts whatever it lands on under the next key pressed.
    await arm();

    const cancel = await screen.findByRole("button", { name: /cancel restore/i });
    await waitFor(() => expect(cancel).toBeEnabled());
    await waitFor(() => expect(cancel).toHaveFocus());
    expect(screen.getByRole("button", { name: /restart now/i })).not.toHaveFocus();
  });
});

describe("finishing the restore from the browser", () => {
  // #386: the last step of a restore used to be a shell command in another window, at the end of a
  // flow that is otherwise entirely in the browser -- and the first-run wizard now opens onto that
  // flow, so a fresh operator met the instruction on their very first screen.

  it("stops Reaper, and claims only that", async () => {
    // The 200 says the server accepted the stop. It does not say Reaper is back, and this page
    // cannot find out: the connection it would ask over is the one going away (rule 85).
    const { person } = await arm();
    const restart = await screen.findByRole("button", { name: /restart now/i });
    await waitFor(() => expect(restart).toBeEnabled());

    await person.click(restart);

    expect(await screen.findByText(/Reaper is stopping/)).toBeInTheDocument();
    expect(apiMock.restoreRestart).toHaveBeenCalledTimes(1);
    // Waited for, not read straight off: this is the second sentence this card speaks, and
    // `announce` drains its queue one `MESSAGE_GAP_MS` apart so neither is lost to a batch.
    await waitFor(() => expect(spoken()).toContain("Reaper is stopping."));
    // Nothing anywhere on the card says it came back, and the button that stops it is gone: a
    // second press would be asking a server that is already going.
    expect(screen.queryByRole("button", { name: /restart now/i })).toBeNull();
    expect(screen.getByRole("button", { name: /reload/i })).toBeInTheDocument();
  });

  it("shows the server's refusal, on a card that had no failure surface at all", async () => {
    // The armed branch rendered a notice and a Cancel and nothing else, so BOTH of its buttons
    // could be refused in silence: a failed cancel set a sentence nothing drew, and the operator
    // watched the button re-enable and never learned the archive was still staged.
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
    // The other side of #387's fix, and the reason it is not applied to every discard: `armed`
    // is server state that outlives this browser, so the card offering Cancel here may never
    // have seen the summary behind it -- a restore armed from a phone shows on the desktop
    // too. A token-scoped Cancel there would refuse the one press that clears it, and an armed
    // restore with no way to cancel is a restore the operator cannot get out of.
    const { person } = await arm();
    const cancel = await screen.findByRole("button", { name: /cancel restore/i });
    await waitFor(() => expect(cancel).toBeEnabled());

    await person.click(cancel);

    // The absence of a token is the assertion, not the argument count: `restoreCancel()` and
    // `restoreCancel(undefined)` are one request on the wire, and pinning which spelling
    // reached the mock would fail on a refactor that changed nothing an operator can see.
    await waitFor(() => expect(apiMock.restoreCancel).toHaveBeenCalled());
    expect(apiMock.restoreCancel.mock.calls[0]?.[0]).toBeUndefined();
  });

  it("asks the server before an unscoped discard, so it cannot take a newer archive", async () => {
    // #387 reached through the one discard a token cannot scope. `armed` is a cached read
    // nothing refreshes from another client, so this card goes on drawing the armed branch
    // after that restore was canceled elsewhere and a fresh archive staged in its place -- and
    // an unconditional discard then destroys the new one. Same shape as the unmount reclaim's
    // guard, and the same reason: reading the cache is the bug, so the SERVER is asked.
    //
    // No invalidation here, deliberately. Handing the card the answer would hide the defect,
    // because the card would simply stop rendering the button under test.
    const { person } = await arm();
    const cancel = await screen.findByRole("button", { name: /cancel restore/i });
    await waitFor(() => expect(cancel).toBeEnabled());
    apiMock.backupInfo.mockResolvedValue({ ...INFO, restore_armed: false });

    await person.click(cancel);

    // The card saying what is true now IS the evidence the server was asked: nothing else in
    // this branch can move it off the armed state. Asserted on the controls rather than on the
    // armed sentence, which the live region is still holding from the confirm, and rather than
    // on a call count, which the refetch behind the redraw makes a moving number.
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
