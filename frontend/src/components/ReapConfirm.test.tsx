// SPDX-License-Identifier: AGPL-3.0-or-later
// The client-side execute gate. These pin the gauntlet the sheet enforces in front of
// the one endpoint that deletes: the button lights only when the dry run proved the
// plan, deletion is armed, and the exact content-bound phrase was typed. Once a reap is
// in flight the sheet shows live progress and a graceful Stop -- and, because the run is
// now detached on the server, the sheet closes freely (the app-wide bar keeps the count
// and Stop), and reopening shows the report.
import { QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, type ReapStatus, type Run, type RunReport } from "../api";
import { testQueryClient } from "../test/queryClient";
import { ReapConfirm } from "./ReapConfirm";

const { apiMock } = vi.hoisted(() => ({
  apiMock: {
    safety: vi.fn(),
    run: vi.fn(),
    dryRun: vi.fn(),
    executeRun: vi.fn(),
    reapStatus: vi.fn(),
    stopRun: vi.fn(),
    plexTrash: vi.fn(),
  },
}));

// Everything but `api` is real -- the sheet reads `ApiError` to tell a moved phrase (409)
// from any other refusal.
vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  api: apiMock,
}));

const run = {
  id: 7,
  snapshot_id: 1,
  policy_hash: "p",
  state: "planned",
  item_count: 1,
  total_bytes: 1024 ** 3,
  held_back_unknown_size: 0,
  confirmation_phrase: "REAP 1 SOUL 1 GB",
  approved_manifest_hash: "m",
  approved_by: "admin",
  approved_at: "2026-01-01T00:00:00+00:00",
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
    error: null,
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
  const utils = render(
    <QueryClientProvider client={queryClient}>
      <ReapConfirm run={run} onClose={onClose} />
    </QueryClientProvider>,
  );
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
  // An empty, fully readable trash: the warning stays out of the way of every test that is
  // about something else. The tests that are about it set their own value.
  apiMock.plexTrash.mockResolvedValue({
    configured: true,
    trashed: 0,
    sections_unreadable: 0,
    empties_after_scan: false,
  });
});

describe("the execute gate", () => {
  it("stays locked until the exact phrase is typed, then lights", async () => {
    const user = userEvent.setup();
    renderSheet();

    await screen.findByText(/Practice run passed/);
    const execute = screen.getByRole("button", { name: /^Reap 1 soul$/ });
    expect(execute).toBeDisabled();

    const input = screen.getByRole("textbox");
    await user.type(input, "REAP 9 SOULS 9 GB"); // a stale tab's phrase
    expect(execute).toBeDisabled();

    await user.clear(input);
    await user.type(input, run.confirmation_phrase);
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
    await user.type(screen.getByRole("textbox"), run.confirmation_phrase);
    const execute = screen.getByRole("button", { name: /^Reap 1 soul$/ });
    expect(execute).toBeDisabled();

    await user.click(screen.getByRole("checkbox"));
    expect(execute).toBeEnabled();
  });

  it("warns when the trash can't be read, rather than reading silence as empty", async () => {
    // Unreadable is Unknown, never Absent: "we could not look" is exactly when the operator
    // most needs telling, so it warns and holds Reap the same way a definite count does.
    apiMock.plexTrash.mockRejectedValue(new Error("plex is down"));
    const user = userEvent.setup();
    renderSheet();

    await screen.findByText(/Practice run passed/);
    expect(await screen.findByText(/couldn't read Plex's trash/i)).toBeInTheDocument();

    await user.type(screen.getByRole("textbox"), run.confirmation_phrase);
    expect(screen.getByRole("button", { name: /^Reap 1 soul$/ })).toBeDisabled();
  });

  it("says nothing at all when the trash is empty and readable", async () => {
    // A warning that fires on every reap stops being read, so the quiet case is silent.
    renderSheet();

    await screen.findByText(/Practice run passed/);
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
    expect(screen.queryByText(/Plex/i)).not.toBeInTheDocument();
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
    expect(screen.getByRole("button", { name: /^Reap 1 soul$/ })).toBeDisabled();
  });

  it("says it couldn't look when the safety read fails, never that deletion is off", async () => {
    apiMock.safety.mockRejectedValue(new Error("safety read failed"));
    renderSheet();

    await screen.findByText(/Practice run passed/);
    expect(await screen.findByText(/couldn't confirm whether deletion is on/)).toBeInTheDocument();
    expect(screen.queryByText(/Deletion is/)).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Reap 1 soul$/ })).toBeDisabled();
  });

  it("a practice run that stopped never unlocks execution", async () => {
    apiMock.dryRun.mockResolvedValue(report({ state: "aborted", aborted_reason: "over the cap" }));
    renderSheet();

    await screen.findByText(/The plan stopped/);
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Reap 1 soul$/ })).not.toBeInTheDocument();
  });

  it("shows live progress and a Stop while reaping, and closes freely (the run is detached)", async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    const { container } = renderSheet(onClose);

    await screen.findByText(/Practice run passed/);
    await user.type(screen.getByRole("textbox"), run.confirmation_phrase);
    await user.click(screen.getByRole("button", { name: /^Reap 1 soul$/ }));

    // In flight: the graceful Stop is offered, and the sheet no longer traps -- the ✕ is
    // enabled and the scrim closes it, because the run keeps going on the server.
    await screen.findByRole("button", { name: /^Stop$/ });
    expect(screen.getByRole("button", { name: "Close" })).toBeEnabled();
    await user.click(container.querySelector(".modal-scrim")!);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("Stop asks the server to halt the run, gracefully", async () => {
    const user = userEvent.setup();
    renderSheet();

    await screen.findByText(/Practice run passed/);
    await user.type(screen.getByRole("textbox"), run.confirmation_phrase);
    await user.click(screen.getByRole("button", { name: /^Reap 1 soul$/ }));

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
    // B-17: a click fires on the nearest common ancestor of press and release, so a drag
    // out of the panel dispatched `click` on the SCRIM and the panel's stopPropagation
    // never saw it. Selecting the confirmation phrase to read or copy it tore the sheet
    // down, taking the practice-run result and anything typed with it.
    const onClose = vi.fn();
    const { container } = renderSheet(onClose);

    const phrase = await screen.findByText(run.confirmation_phrase);
    const scrim = container.querySelector(".modal-scrim")!;

    fireEvent.mouseDown(phrase);
    fireEvent.mouseUp(scrim);
    fireEvent.click(scrim); // what the browser dispatches at the common ancestor
    expect(onClose).not.toHaveBeenCalled();

    // A real click outside still closes: pressed AND released on the scrim itself.
    fireEvent.mouseDown(scrim);
    fireEvent.mouseUp(scrim);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("is a dialog that says what it is", async () => {
    renderSheet();

    const dialog = await screen.findByRole("dialog", { name: /Reap 1 soul/ });
    expect(dialog).toHaveAttribute("aria-modal", "true");
  });

  it("sends the phrase the operator typed, not the copy it was handed", async () => {
    const user = userEvent.setup();
    renderSheet();

    await screen.findByText(/Practice run passed/);
    // Trailing whitespace is trimmed on the way out, which is only possible if what is
    // posted comes from the input. Echoing the prop would make the human gate a `disabled`
    // attribute the server cannot tell from a script (S-1).
    await user.type(screen.getByRole("textbox"), `${run.confirmation_phrase}  `);
    await user.click(screen.getByRole("button", { name: /^Reap 1 soul$/ }));
    expect(apiMock.executeRun).toHaveBeenCalledWith(run.id, run.confirmation_phrase);
  });

  it("re-measures against the phrase the server moved to", async () => {
    const user = userEvent.setup();
    apiMock.executeRun.mockRejectedValue(new ApiError(409, "The plan changed."));
    // What the server holds now: a soul more, so a different phrase.
    const moved = { ...run, item_count: 2, confirmation_phrase: "REAP 2 SOULS 1 GB" };
    apiMock.run.mockResolvedValue(moved);
    renderSheet();

    await screen.findByText(/Practice run passed/);
    await user.type(screen.getByRole("textbox"), run.confirmation_phrase);
    await user.click(screen.getByRole("button", { name: /^Reap 1 soul$/ }));
    await screen.findByText(/The plan changed./);

    // Otherwise the sheet deadlocks: it keeps lighting the button for the stale phrase the
    // server now refuses, and typing the real one disables it (S-1). This is rendered from a
    // run the caller only CAPTURED -- what "Reap now" in the review queue hands over -- so
    // nothing outside the sheet observes ["run", id], and the invalidation reached nobody at
    // all until the sheet started watching that key itself.
    const input = await screen.findByRole("textbox");
    expect((input as HTMLInputElement).placeholder).toBe(moved.confirmation_phrase);
    expect(screen.getByRole("button", { name: /^Reap 2 souls$/ })).toBeInTheDocument();
  });

  it("says a reap stopped on a problem, and never re-arms itself in silence", async () => {
    // The executor raised mid-run: no report, and files may already be gone. The confirm
    // stage must not come back live with the phrase still typed (B-3).
    const failed = status({
      run_id: run.id,
      phase: "error",
      error: "Deletion was switched off mid-run.",
    });
    apiMock.reapStatus.mockResolvedValue(failed);
    apiMock.dryRun.mockClear();
    renderSheet(() => {}, failed);

    expect(await screen.findByText(/The reap stopped on a problem/)).toBeInTheDocument();
    expect(screen.getByText(/Deletion was switched off mid-run./)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Done" })).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Reap 1 soul$/ })).not.toBeInTheDocument();
    // And no dry run is fired over it: the executor refuses one on a non-planned run, and
    // its blurb would render as the only explanation of a failed deletion.
    expect(apiMock.dryRun).not.toHaveBeenCalled();
  });

  it("shows the per-item checklist once the run finishes", async () => {
    const user = userEvent.setup();
    const { queryClient } = renderSheet();

    await screen.findByText(/Practice run passed/);
    await user.type(screen.getByRole("textbox"), run.confirmation_phrase);
    await user.click(screen.getByRole("button", { name: /^Reap 1 soul$/ }));
    await screen.findByRole("button", { name: /^Stop$/ });

    // The run finishes: the status carries the after-action report.
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
                detail: "deleted",
                checks: [{ label: "Nobody was watching it right now", ok: true }],
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
