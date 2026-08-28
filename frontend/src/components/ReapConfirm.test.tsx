// SPDX-License-Identifier: AGPL-3.0-or-later
// This file pins the client-side execute gate. These tests check what the sheet enforces in
// front of the one endpoint that deletes. The button lights only when the dry run proves the
// plan, deletion is armed, and the exact content-bound phrase is typed. Once a reap is in
// flight, the sheet shows live progress and a graceful Stop. Because the run is now detached
// on the server, the sheet closes freely, the app-wide bar keeps the count and Stop, and
// reopening it shows the report.
import { act, fireEvent, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, type ReapStatus, type Run, type RunReport } from "../api";
import { fill } from "../test/forms";
import { testQueryClient } from "../test/queryClient";
import { renderWithProviders } from "../test/renderWithProviders";
import { expectNoA11yViolations } from "../test/a11y";
import { Announcer } from "../announce";
import { ReapConfirm } from "./ReapConfirm";

const { apiMock } = await vi.hoisted(async () => ({
  apiMock: (await import("../test/apiMock")).makeApiMock(),
}));

// Everything but `api` is real. The sheet reads `ApiError` to tell a moved phrase (409) apart
// from any other refusal.
vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  api: apiMock,
}));

const run = {
  id: 7,
  snapshot_id: 1,
  state: "planned",
  item_count: 1,
  total_bytes: 1024 ** 3,
  held_back_unknown_size: 0,
  confirmation_phrase: "REAP 1 SOUL 1 GB",
  step_count: 0,
  steps: [],
} as Run;

function report(overrides: Partial<RunReport> = {}): RunReport {
  return {
    run_id: run.id,
    dry_run: true,
    state: "completed",
    aborted_reason: null,
    would_delete_items: 1,
    deleted_bytes: 0,
    deleted_unmeasured: 0,
    skipped: 0,
    outcomes: [],
    ...overrides,
  };
}

function status(overrides: Partial<ReapStatus> = {}): ReapStatus {
  return {
    running: false,
    run_id: null,
    stopping: false,
    phase: "idle",
    done: 0,
    total: 0,
    deleted_items: 0,
    deleted_bytes: 0,
    skipped: 0,
    title: "",
    error_reason: null,
    report: null,
    ...overrides,
  };
}

const runningStatus = status({ running: true, run_id: run.id, phase: "reaping", total: 1 });

function renderSheet(onClose: () => void = () => {}, seedStatus?: ReapStatus) {
  const queryClient = testQueryClient();
  // The status cache is shared with the app-wide bar and is already warm when this sheet is
  // opened from it. Seeding it reproduces that, which is what the dry-run skip reads.
  if (seedStatus) queryClient.setQueryData(["reapStatus"], seedStatus);
  const utils = renderWithProviders(<ReapConfirm run={run} onClose={onClose} />, {
    client: queryClient,
  });
  return { ...utils, queryClient };
}

beforeEach(() => {
  vi.restoreAllMocks();
  apiMock.safety.mockResolvedValue({ destructive_enabled: true });
  apiMock.run.mockResolvedValue(run); // only ever fetched after a 409 moves the phrase
  apiMock.dryRun.mockResolvedValue(report());
  apiMock.reapStatus.mockResolvedValue(status()); // idle until a reap starts
  apiMock.executeRun.mockResolvedValue(runningStatus);
  apiMock.stopRun.mockResolvedValue({ ...runningStatus, stopping: true });
  // The default trash is empty and fully readable, so the warning stays out of the way of
  // every test that is about something else. Tests that are about the warning set their own
  // value.
  apiMock.plexTrash.mockResolvedValue({
    configured: true,
    trashed: 0,
    sections_unreadable: 0,
    empties_after_scan: false,
  });
});

describe("the execute gate", () => {
  // This sheet is the last surface in front of the one route that deletes, so what a screen
  // reader makes of it is a safety property. An operator who cannot hear why Reap is locked
  // cannot tell a working gate from a broken button. axe reads the tree the browser built, so
  // the phrase box and the trash warning are judged as rendered, not as written.
  it("has no accessibility violations", async () => {
    const { container } = renderSheet();
    await screen.findByText(/Practice run passed/);
    await expectNoA11yViolations(container);
  });

  it("stays locked until the exact phrase is typed, then lights", async () => {
    const user = userEvent.setup();
    renderSheet();

    await screen.findByText(/Practice run passed/);
    const execute = screen.getByRole("button", { name: /^Reap$/ });
    expect(execute).toBeDisabled();

    const input = screen.getByRole("textbox");
    await fill(user, input, "REAP 9 SOULS 9 GB"); // a stale tab's phrase
    expect(execute).toBeDisabled();

    await fill(user, input, run.confirmation_phrase);
    expect(execute).toBeEnabled();
  });

  // Emptying Plex's trash takes the library records of everything already in there, not
  // just what this run deleted, and those items cancel out of the executor's before/after
  // count so its gate cannot see them. The operator is told, and must say yes on purpose.
  it("holds Reap until the operator accepts that Plex's trash goes too", async () => {
    apiMock.plexTrash.mockResolvedValue({
      configured: true,
      trashed: 40,
      sections_unreadable: 0,
      empties_after_scan: false,
    });
    const user = userEvent.setup();
    renderSheet();

    await screen.findByText(/Practice run passed/);
    expect(await screen.findByText(/already holds 40 items/i)).toBeInTheDocument();

    // The phrase alone is not enough while the warning stands.
    await fill(user, screen.getByRole("textbox"), run.confirmation_phrase);
    const execute = screen.getByRole("button", { name: /^Reap$/ });
    expect(execute).toBeDisabled();

    await user.click(screen.getByRole("checkbox"));
    expect(execute).toBeEnabled();
  });

  it("warns when the trash can't be read, rather than reading silence as empty", async () => {
    // Unreadable counts as unknown, not absent. The moment the app could not check is exactly
    // when the operator needs telling, so it warns and holds Reap the same way a definite
    // count does.
    apiMock.plexTrash.mockRejectedValue(new Error("plex is down"));
    const user = userEvent.setup();
    renderSheet();

    await screen.findByText(/Practice run passed/);
    expect(await screen.findByText(/couldn't read Plex's trash/i)).toBeInTheDocument();

    await fill(user, screen.getByRole("textbox"), run.confirmation_phrase);
    expect(screen.getByRole("button", { name: /^Reap$/ })).toBeDisabled();
  });

  it("says nothing at all when the trash is empty and readable", async () => {
    // A warning that fires on every reap stops being read, so the quiet case is silent.
    renderSheet();

    await screen.findByText(/Practice run passed/);
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
    expect(screen.queryByText(/Plex/i)).not.toBeInTheDocument();
  });

  it("stays silent when Plex empties its own trash and there is nothing in it", async () => {
    // Plex ships this preference on by default, and most servers never change it. Shown on its
    // own, it is not a warning, it would stand in front of every reap and train the operator to
    // ignore the one that matters. It only appears alongside a trash count that already
    // warrants telling them.
    apiMock.plexTrash.mockResolvedValue({
      configured: true,
      trashed: 0,
      sections_unreadable: 0,
      empties_after_scan: true,
    });
    renderSheet();

    await screen.findByText(/Practice run passed/);
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
    expect(screen.queryByText(/empty its trash/i)).not.toBeInTheDocument();
  });

  it("says who does the emptying when the trash already holds records", async () => {
    apiMock.plexTrash.mockResolvedValue({
      configured: true,
      trashed: 40,
      sections_unreadable: 0,
      empties_after_scan: true,
    });
    renderSheet();

    await screen.findByText(/Practice run passed/);
    expect(await screen.findByText(/already holds 40 items/i)).toBeInTheDocument();
    expect(screen.getByText(/empty its trash after every scan/i)).toBeInTheDocument();
  });

  it("says nothing about auto-emptying when the preference could not be read", async () => {
    // `null` means unknown. Reading it as "Plex does not empty its own trash" would wrongly
    // reassure the operator about a preference nobody actually checked.
    apiMock.plexTrash.mockResolvedValue({
      configured: true,
      trashed: 40,
      sections_unreadable: 0,
      empties_after_scan: null,
    });
    renderSheet();

    await screen.findByText(/Practice run passed/);
    expect(await screen.findByText(/already holds 40 items/i)).toBeInTheDocument();
    expect(screen.queryByText(/empty its trash/i)).not.toBeInTheDocument();
  });

  it("offers no phrase input at all while deletion is off", async () => {
    apiMock.safety.mockResolvedValue({ destructive_enabled: false });
    renderSheet();

    await screen.findByText(/Practice run passed/);
    expect(await screen.findByText(/Deletion is/)).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("says it is still checking while the safety read is in flight", async () => {
    apiMock.safety.mockImplementation(() => new Promise(() => {})); // never settles
    renderSheet();

    await screen.findByText(/Practice run passed/);
    expect(await screen.findByText(/Checking whether deletion is on/)).toBeInTheDocument();
    expect(screen.queryByText(/Deletion is/)).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Reap$/ })).toBeDisabled();
  });

  it("says it couldn't look when the safety read fails, never that deletion is off", async () => {
    apiMock.safety.mockRejectedValue(new Error("safety read failed"));
    renderSheet();

    await screen.findByText(/Practice run passed/);
    expect(await screen.findByText(/couldn't confirm whether deletion is on/)).toBeInTheDocument();
    expect(screen.queryByText(/Deletion is/)).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Reap$/ })).toBeDisabled();
  });

  it("a practice run that stopped never unlocks execution", async () => {
    apiMock.dryRun.mockResolvedValue(
      report({ state: "aborted", aborted_reason: { k: "legacy", p: { text: "over the cap" } } }),
    );
    renderSheet();

    await screen.findByText(/The plan stopped/);
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Reap$/ })).not.toBeInTheDocument();
  });

  it("shows live progress and a Stop while reaping, and closes freely (the run is detached)", async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    const { container } = renderSheet(onClose);

    await screen.findByText(/Practice run passed/);
    await fill(user, screen.getByRole("textbox"), run.confirmation_phrase);
    await user.click(screen.getByRole("button", { name: /^Reap$/ }));

    // While the reap is in flight, the graceful Stop is offered and the sheet no longer traps
    // focus. The close button is enabled and the scrim closes it, because the run keeps going
    // on the server.
    await screen.findByRole("button", { name: /^Stop$/ });
    expect(screen.getByRole("button", { name: "Close" })).toBeEnabled();
    await user.click(container.querySelector(".modal-scrim")!);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("Stop asks the server to halt the run, gracefully", async () => {
    const user = userEvent.setup();
    renderSheet();

    await screen.findByText(/Practice run passed/);
    await fill(user, screen.getByRole("textbox"), run.confirmation_phrase);
    await user.click(screen.getByRole("button", { name: /^Reap$/ }));

    await user.click(await screen.findByRole("button", { name: /^Stop$/ }));
    expect(apiMock.stopRun).toHaveBeenCalledWith(run.id);
    // Once stopping, the button says so and no longer offers a second Stop.
    expect(await screen.findByRole("button", { name: /Stopping/ })).toBeDisabled();
  });

  it("closes on Escape", async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    renderSheet(onClose);

    await screen.findByText(/Practice run passed/);
    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("survives a drag that starts on the phrase and ends outside the panel", async () => {
    // A browser click event fires on the nearest common ancestor of the press and the release.
    // A drag that starts inside the panel and ends on the scrim therefore dispatches a click on
    // the scrim, which the panel's stopPropagation never sees. Selecting the confirmation
    // phrase to read or copy it must not close the sheet this way, since that would lose the
    // practice-run result and anything already typed.
    const onClose = vi.fn();
    const { container } = renderSheet(onClose);

    const phrase = await screen.findByText(run.confirmation_phrase);
    const scrim = container.querySelector(".modal-scrim")!;

    fireEvent.mouseDown(phrase);
    fireEvent.mouseUp(scrim);
    fireEvent.click(scrim); // what the browser dispatches at the common ancestor
    expect(onClose).not.toHaveBeenCalled();

    // A real click outside the panel, pressed and released on the scrim itself, still closes it.
    fireEvent.mouseDown(scrim);
    fireEvent.mouseUp(scrim);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("is a dialog that says what it is", async () => {
    renderSheet();

    const dialog = await screen.findByRole("dialog", { name: /Reap 1 title/ });
    expect(dialog).toHaveAttribute("aria-modal", "true");
  });

  it("sends the phrase the operator typed, not the copy it was handed", async () => {
    const user = userEvent.setup();
    renderSheet();

    await screen.findByText(/Practice run passed/);
    // Trailing whitespace is trimmed on the way out, which is only possible if the posted
    // value comes from the input box. If the sheet echoed the prop instead, the human check
    // here would be reduced to a `disabled` attribute, which the server cannot tell apart from
    // a script bypassing it.
    await fill(user, screen.getByRole("textbox"), `${run.confirmation_phrase}  `);
    await user.click(screen.getByRole("button", { name: /^Reap$/ }));
    expect(apiMock.executeRun).toHaveBeenCalledWith(run.id, run.confirmation_phrase);
  });

  it("re-measures against the phrase the server moved to", async () => {
    const user = userEvent.setup();
    apiMock.executeRun.mockRejectedValue(new ApiError(409, "The plan changed."));
    // The server now holds one more soul, so it expects a different phrase.
    const moved = { ...run, item_count: 2, confirmation_phrase: "REAP 2 SOULS 1 GB" };
    apiMock.run.mockResolvedValue(moved);
    renderSheet();

    await screen.findByText(/Practice run passed/);
    await fill(user, screen.getByRole("textbox"), run.confirmation_phrase);
    await user.click(screen.getByRole("button", { name: /^Reap$/ }));
    await screen.findByText(/The plan changed./);

    // Without this, the sheet would deadlock. It would keep lighting the button for the stale
    // phrase the server now refuses, while typing the real one disables it. The sheet renders
    // from a run object the caller only captured, the same one "Reap now" in the review queue
    // hands over, so nothing outside the sheet observes the query key ["run", id]. The sheet
    // has to watch that key itself, or the invalidation reaches nobody.
    await screen.findByRole("textbox");
    expect(screen.getByText(moved.confirmation_phrase)).toBeInTheDocument();
    expect(screen.queryByText(run.confirmation_phrase)).not.toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: /Reap 2 titles/ })).toBeInTheDocument();
  });

  it("says a reap stopped on a problem, and never re-arms itself in silence", async () => {
    // The executor raised an error mid-run, so there is no report and files may already be
    // gone. The confirm stage must not come back live with the phrase still typed.
    const failed = status({
      run_id: run.id,
      phase: "error",
      error_reason: {
        k: "error.reap.unexpected",
        p: { error: "Deletion was switched off mid-run." },
      },
    });
    apiMock.reapStatus.mockResolvedValue(failed);
    apiMock.dryRun.mockClear();
    renderSheet(() => {}, failed);

    expect(await screen.findByText(/The reap stopped on a problem/)).toBeInTheDocument();
    expect(screen.getByText(/Deletion was switched off mid-run./)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Done" })).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Reap$/ })).not.toBeInTheDocument();
    // No dry run is fired over this state. The executor refuses one on a non-planned run, and
    // a dry-run message would otherwise render as the only explanation for a failed deletion.
    expect(apiMock.dryRun).not.toHaveBeenCalled();
  });

  it("shows the per-item checklist once the run finishes", async () => {
    const user = userEvent.setup();
    const { queryClient } = renderSheet();

    await screen.findByText(/Practice run passed/);
    await fill(user, screen.getByRole("textbox"), run.confirmation_phrase);
    await user.click(screen.getByRole("button", { name: /^Reap$/ }));
    await screen.findByRole("button", { name: /^Stop$/ });

    // The run finishes, and the status now carries the after-action report.
    act(() => {
      queryClient.setQueryData(
        ["reapStatus"],
        status({
          running: false,
          run_id: run.id,
          phase: "complete",
          deleted_items: 1,
          deleted_bytes: 1024 ** 3,
          report: report({
            dry_run: false,
            would_delete_items: 1,
            deleted_bytes: 1024 ** 3,
            outcomes: [
              {
                media_key: "radarr:1:1",
                title: "A Film",
                kind: "radarr_delete",
                state: "verified",
                detail_reason: { k: "legacy", p: { text: "deleted" } },
                checks: [
                  {
                    label_reason: {
                      k: "legacy",
                      p: { text: "Nobody was watching it right now" },
                    },
                    ok: true,
                  },
                ],
                is_canary: false,
              },
            ],
          }),
        }),
      );
    });

    await screen.findByText(/1 soul reclaimed/);
    expect(screen.getByText("A Film")).toBeInTheDocument();
    expect(screen.getByText(/Nobody was watching it right now/)).toBeInTheDocument();
  });
});

// This sheet is the one surface in the app that starts a deletion, so it must keep speaking
// throughout the whole run, not just once at the start. `ModalShell` announces the dialog by
// name only when it opens. After that the body changes on a poll, moving through the practice
// run, the typed-phrase field arriving, progress, then a report, and each of those changes
// needs its own live region or focus move.
describe("what a screen reader hears while a reap runs", () => {
  /** What the app's polite live region is holding. `Announcer` lives at the app root, and this
   *  sheet renders without it, so these assertions read the announcement store through a region
   *  mounted beside the sheet instead. That is the same thing an operator would hear, and the
   *  only way to observe it from here. */
  function spoken(): string {
    return screen
      .getAllByRole("status")
      .map((r) => r.textContent)
      .filter(Boolean)
      .join(" | ");
  }

  function renderWithAnnouncer(seedStatus?: ReapStatus) {
    const queryClient = testQueryClient();
    if (seedStatus) queryClient.setQueryData(["reapStatus"], seedStatus);
    const utils = renderWithProviders(
      <>
        <Announcer />
        <ReapConfirm run={run} onClose={() => {}} />
      </>,
      { client: queryClient },
    );
    return { ...utils, queryClient };
  }

  it("says the practice run passed and what to do next", async () => {
    renderWithAnnouncer();

    await screen.findByText(/Practice run passed/);
    expect(spoken()).toContain("Type the confirmation phrase");
  });

  it("puts the operator in the phrase box when it appears", async () => {
    // The box arrives part-way through a dialog they are already standing in, on the practice
    // run settling rather than on anything they did. Nothing carried them to it, and it is the
    // last gate before files are deleted.
    renderWithAnnouncer();

    const input = await screen.findByRole("textbox");
    expect(input).toHaveFocus();
  });

  it("says deletion is off rather than leaving the missing box unexplained", async () => {
    apiMock.safety.mockResolvedValue({ destructive_enabled: false });
    renderWithAnnouncer();

    await screen.findByText(/Deletion is/);
    expect(spoken()).toContain("deletion is off");
  });

  it("never says deletion is off about a switch it could not read", async () => {
    // `armed` is `destructive_enabled === true`, so a failed read must not collapse into "off".
    // Announcing "off" as a definite fact on the last screen before files are deleted would be
    // the wrong direction to guess, even though the on-screen block keeps all three states apart.
    apiMock.safety.mockImplementation(() => Promise.reject(new Error("unreachable")));
    renderWithAnnouncer();

    await screen.findByText(/couldn't confirm whether deletion is on/i);
    expect(spoken()).toContain("couldn't confirm whether deletion is on");
    expect(spoken()).not.toContain("deletion is off");
  });

  it("does not send the operator to a phrase box another reap is holding shut", async () => {
    // A dry run does not claim the one execute slot, so the practice run can pass while someone
    // else's reap holds it. The screen then says to come back later and renders no phrase
    // field, so the announcement must not tell the operator to type one. `say` also dedupes on
    // the exact sentence, so if the announcement text were the same each time, the correct line
    // would be swallowed as a repeat once the field actually arrives.
    const elsewhere = status({ running: true, run_id: 99, phase: "reaping", total: 1 });
    apiMock.reapStatus.mockResolvedValue(elsewhere);
    renderWithAnnouncer(elsewhere);

    await screen.findByText(/Another reap is running/);
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(spoken()).toContain("Another reap is running");
    expect(spoken()).not.toContain("Type the confirmation phrase");
  });

  it("leaves focus on the Plex-trash consent that is holding Reap disabled", async () => {
    // The notice renders above the phrase field, and its checkbox is what keeps Reap disabled.
    // Jumping the operator straight into the phrase box would hide both the notice and the
    // reason the button will not light. They would type the exact phrase, find Reap still
    // disabled, and never learn why.
    apiMock.plexTrash.mockResolvedValue({
      configured: true,
      trashed: 40,
      sections_unreadable: 0,
      empties_after_scan: false,
    });
    renderWithAnnouncer();

    const input = await screen.findByRole("textbox");
    expect(input).not.toHaveFocus();
    // And the thing they need is still ahead of them in reading order.
    const consent = screen.getByRole("checkbox");
    expect(consent.compareDocumentPosition(input)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
  });

  it("states the progress as a progressbar, in words rather than a bare number", async () => {
    const half = status({ running: true, run_id: run.id, phase: "reaping", done: 2, total: 4 });
    apiMock.reapStatus.mockResolvedValue(half);
    renderWithAnnouncer(half);

    const bar = await screen.findByRole("progressbar", { name: "Reaping" });
    expect(bar).toHaveAttribute("aria-valuenow", "50");
    expect(bar).toHaveAttribute("aria-valuetext", "50%, 2 of 4 removed");
  });

  it("announces progress in tenths, not once per item", async () => {
    // The status polls every second. A sentence per item on a run of hundreds would hold the
    // app's one polite region for the length of the run, so nothing else could be heard.
    const at = (done: number, total: number) =>
      status({ running: true, run_id: run.id, phase: "reaping", done, total });
    apiMock.reapStatus.mockResolvedValue(at(0, 100));
    const { queryClient } = renderWithAnnouncer(at(0, 100));
    await screen.findByRole("progressbar", { name: "Reaping" });

    const heard: string[] = [];
    for (const done of [1, 2, 3, 4, 5]) {
      act(() => void queryClient.setQueryData(["reapStatus"], at(done, 100)));
      heard.push(spoken());
    }
    // Five items, all inside the first tenth, produce one sentence rather than five.
    expect(new Set(heard).size).toBe(1);
    expect(heard[0]).toContain("0% deleted");

    // Crossing into the next tenth does speak. This uses `findByText` rather than a
    // synchronous read, because the announcer holds each sentence for its turn before the next
    // one may replace it, so the new sentence arrives a beat later by design (announce.tsx).
    act(() => void queryClient.setQueryData(["reapStatus"], at(10, 100)));
    expect(await screen.findByText("10% deleted.")).toBeInTheDocument();
  });

  it("moves focus to the outcome when the run ends, because the dialog's job has changed", async () => {
    const { queryClient } = renderWithAnnouncer();
    await screen.findByRole("textbox");

    act(() => {
      queryClient.setQueryData(
        ["reapStatus"],
        status({
          run_id: run.id,
          phase: "complete",
          deleted_items: 1,
          deleted_bytes: 1024 ** 3,
          report: report({ dry_run: false, would_delete_items: 1, deleted_bytes: 1024 ** 3 }),
        }),
      );
    });

    const outcome = (await screen.findByText(/1 soul reclaimed/)).closest(".reap-result");
    expect(outcome).toHaveFocus();
  });

  it("moves focus to the failure, which is the only account of files already gone", async () => {
    // This starts from a healthy sheet that then fails underneath the operator. That is the
    // shape that matters here, the run raising an error while the operator is still standing in
    // the confirm stage.
    const { queryClient } = renderWithAnnouncer();
    await screen.findByRole("textbox");

    act(
      () =>
        void queryClient.setQueryData(
          ["reapStatus"],
          status({
            run_id: run.id,
            phase: "error",
            error_reason: {
              k: "error.reap.unexpected",
              p: { error: "Deletion was switched off mid-run." },
            },
          }),
        ),
    );

    const block = (await screen.findByText(/The reap stopped on a problem/)).closest(".reap-arm");
    expect(block).toHaveFocus();
  });

  it("tells a pass from a fail in the report, where the glyph alone cannot", async () => {
    // ✓ and ✗ are both silent on NVDA at its default symbol level, so the two lines would read
    // out identically in the report for a run that has just deleted files.
    const done = status({
      run_id: run.id,
      phase: "complete",
      deleted_items: 1,
      report: report({
        dry_run: false,
        outcomes: [
          {
            media_key: "radarr:1:1",
            title: "A Film",
            kind: "radarr_delete",
            state: "verified",
            detail_reason: { k: "legacy", p: { text: "deleted" } },
            checks: [
              {
                label_reason: { k: "legacy", p: { text: "Nobody was watching it right now" } },
                ok: true,
              },
              {
                label_reason: {
                  k: "legacy",
                  p: { text: "It was played since you approved it" },
                },
                ok: false,
              },
            ],
            is_canary: false,
          },
        ],
      }),
    });
    apiMock.reapStatus.mockResolvedValue(done);
    renderWithAnnouncer(done);

    const passed = await screen.findByText(/Nobody was watching it right now/);
    expect(passed.closest("li")).toHaveTextContent("Passed: Nobody was watching it right now");
    const failedCheck = screen.getByText(/It was played since you approved it/);
    expect(failedCheck.closest("li")).toHaveTextContent(
      "Failed: It was played since you approved it",
    );
  });
});
