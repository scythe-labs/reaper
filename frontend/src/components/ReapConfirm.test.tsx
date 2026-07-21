// SPDX-License-Identifier: AGPL-3.0-or-later
// The client-side execute gate. These pin the gauntlet the sheet enforces in front of
// the one endpoint that deletes: the button lights only when the dry run proved the
// plan, deletion is armed, and the exact content-bound phrase was typed. Once a reap is
// in flight the sheet shows live progress and a graceful Stop -- and, because the run is
// now detached on the server, the sheet closes freely (the app-wide bar keeps the count
// and Stop), and reopening shows the report.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ReapStatus, Run, RunReport } from "../api";
import { ReapConfirm } from "./ReapConfirm";

const { apiMock } = vi.hoisted(() => ({
  apiMock: {
    safety: vi.fn(),
    dryRun: vi.fn(),
    executeRun: vi.fn(),
    reapStatus: vi.fn(),
    stopRun: vi.fn(),
  },
}));

vi.mock("../api", () => ({ api: apiMock }));

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

function renderSheet(onClose: () => void = () => {}) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
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
  apiMock.dryRun.mockResolvedValue(report());
  apiMock.reapStatus.mockResolvedValue(status()); // idle until a reap starts
  apiMock.executeRun.mockResolvedValue(runningStatus);
  apiMock.stopRun.mockResolvedValue({ ...runningStatus, stopping: true });
});

describe("the execute gate", () => {
  it("stays locked until the exact phrase is typed, then lights", async () => {
    const user = userEvent.setup();
    renderSheet();

    await screen.findByText(/Dry run passed/);
    const execute = screen.getByRole("button", { name: /^Reap 1 soul$/ });
    expect(execute).toBeDisabled();

    const input = screen.getByRole("textbox");
    await user.type(input, "REAP 9 SOULS 9 GB"); // a stale tab's phrase
    expect(execute).toBeDisabled();

    await user.clear(input);
    await user.type(input, run.confirmation_phrase);
    expect(execute).toBeEnabled();
  });

  it("offers no phrase input at all while deletion is off", async () => {
    apiMock.safety.mockResolvedValue({ destructive_enabled: false });
    renderSheet();

    await screen.findByText(/Dry run passed/);
    expect(await screen.findByText(/Deletion is/)).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("says it is still checking while the safety read is in flight", async () => {
    apiMock.safety.mockImplementation(() => new Promise(() => {})); // never settles
    renderSheet();

    await screen.findByText(/Dry run passed/);
    expect(await screen.findByText(/Checking whether deletion is on/)).toBeInTheDocument();
    expect(screen.queryByText(/Deletion is/)).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Reap 1 soul$/ })).toBeDisabled();
  });

  it("says it couldn't look when the safety read fails, never that deletion is off", async () => {
    apiMock.safety.mockRejectedValue(new Error("safety read failed"));
    renderSheet();

    await screen.findByText(/Dry run passed/);
    expect(await screen.findByText(/couldn't confirm whether deletion is on/)).toBeInTheDocument();
    expect(screen.queryByText(/Deletion is/)).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Reap 1 soul$/ })).toBeDisabled();
  });

  it("an aborted dry run never unlocks execution", async () => {
    apiMock.dryRun.mockResolvedValue(
      report({ state: "aborted", aborted_reason: "over the cap" }),
    );
    renderSheet();

    await screen.findByText(/The plan aborted/);
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /^Reap 1 soul$/ }),
    ).not.toBeInTheDocument();
  });

  it("shows live progress and a Stop while reaping, and closes freely (the run is detached)", async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    const { container } = renderSheet(onClose);

    await screen.findByText(/Dry run passed/);
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

    await screen.findByText(/Dry run passed/);
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

    await screen.findByText(/Dry run passed/);
    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("is a dialog that says what it is", async () => {
    renderSheet();

    const dialog = await screen.findByRole("dialog", { name: /Reap 1 soul/ });
    expect(dialog).toHaveAttribute("aria-modal", "true");
  });

  it("shows the per-item checklist once the run finishes", async () => {
    const user = userEvent.setup();
    const { queryClient } = renderSheet();

    await screen.findByText(/Dry run passed/);
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
