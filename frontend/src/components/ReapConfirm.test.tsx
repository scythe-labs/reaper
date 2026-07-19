// SPDX-License-Identifier: AGPL-3.0-or-later
// The client-side execute gate. These pin the gauntlet the sheet enforces in front of
// the one endpoint that deletes: the button lights only when the dry run proved the
// plan, deletion is armed, and the exact content-bound phrase was typed -- and the
// sheet cannot be dismissed while a real reap is in flight (closing would lose the
// per-item report of what was just deleted).
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Run, RunReport } from "../api";
import { ReapConfirm } from "./ReapConfirm";

const { apiMock } = vi.hoisted(() => ({
  apiMock: {
    safety: vi.fn(),
    dryRun: vi.fn(),
    executeRun: vi.fn(),
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
  confirmation_phrase: "REAP 1 ITEMS 1 GB",
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
    skipped: 0,
    outcomes: [],
    ...overrides,
  };
}

function renderSheet(onClose: () => void = () => {}) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <ReapConfirm run={run} onClose={onClose} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.restoreAllMocks();
  apiMock.safety.mockResolvedValue({ destructive_enabled: true });
  apiMock.dryRun.mockResolvedValue(report());
  apiMock.executeRun.mockResolvedValue(report({ dry_run: false }));
});

describe("the execute gate", () => {
  it("stays locked until the exact phrase is typed, then lights", async () => {
    const user = userEvent.setup();
    renderSheet();

    await screen.findByText(/Dry run passed/);
    const execute = screen.getByRole("button", { name: /^Reap 1 items$/ });
    expect(execute).toBeDisabled();

    const input = screen.getByRole("textbox");
    await user.type(input, "REAP 9 ITEMS 9 GB"); // a stale tab's phrase
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
    expect(screen.getByRole("button", { name: /^Reap 1 items$/ })).toBeDisabled();
  });

  it("says it couldn't look when the safety read fails, never that deletion is off", async () => {
    apiMock.safety.mockRejectedValue(new Error("safety read failed"));
    renderSheet();

    await screen.findByText(/Dry run passed/);
    expect(await screen.findByText(/couldn't confirm whether deletion is on/)).toBeInTheDocument();
    expect(screen.queryByText(/Deletion is/)).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Reap 1 items$/ })).toBeDisabled();
  });

  it("an aborted dry run never unlocks execution", async () => {
    apiMock.dryRun.mockResolvedValue(
      report({ state: "aborted", aborted_reason: "over the cap" }),
    );
    renderSheet();

    await screen.findByText(/The plan aborted/);
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /^Reap 1 items$/ }),
    ).not.toBeInTheDocument();
  });

  it("cannot be dismissed while the reap is in flight", async () => {
    let finish: (r: RunReport) => void = () => {};
    apiMock.executeRun.mockImplementation(
      () => new Promise<RunReport>((resolve) => (finish = resolve)),
    );
    const onClose = vi.fn();
    const user = userEvent.setup();
    const { container } = renderSheet(onClose);

    await screen.findByText(/Dry run passed/);
    await user.type(screen.getByRole("textbox"), run.confirmation_phrase);
    await user.click(screen.getByRole("button", { name: /^Reap 1 items$/ }));

    // Mid-flight: scrim, ✕ and Cancel must all refuse to unmount the sheet.
    await screen.findByRole("button", { name: /Reaping/ });
    await user.click(container.querySelector(".modal-scrim")!);
    expect(onClose).not.toHaveBeenCalled();
    const closeX = screen.getByRole("button", { name: "Close" });
    expect(closeX).toBeDisabled();

    // Once the report is in, Done closes normally.
    finish(report({ dry_run: false }));
    await user.click(await screen.findByRole("button", { name: "Done" }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("cannot be dismissed with Escape while the reap is in flight", async () => {
    let finish: (r: RunReport) => void = () => {};
    apiMock.executeRun.mockImplementation(
      () => new Promise<RunReport>((resolve) => (finish = resolve)),
    );
    const onClose = vi.fn();
    const user = userEvent.setup();
    renderSheet(onClose);

    await screen.findByText(/Dry run passed/);
    await user.type(screen.getByRole("textbox"), run.confirmation_phrase);
    await user.click(screen.getByRole("button", { name: /^Reap 1 items$/ }));

    await screen.findByRole("button", { name: /Reaping/ });
    await user.keyboard("{Escape}");
    expect(onClose).not.toHaveBeenCalled();

    finish(report({ dry_run: false }));
    await screen.findByText(/reclaimed/);
  });

  it("closes on Escape while no reap is in flight", async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    renderSheet(onClose);

    await screen.findByText(/Dry run passed/);
    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("is a dialog that says what it is", async () => {
    renderSheet();

    const dialog = await screen.findByRole("dialog", { name: /Reap 1 items/ });
    expect(dialog).toHaveAttribute("aria-modal", "true");
  });

  it("shows the per-item checklist after a real reap", async () => {
    apiMock.executeRun.mockResolvedValue(
      report({
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
    );
    const user = userEvent.setup();
    renderSheet();

    await screen.findByText(/Dry run passed/);
    await user.type(screen.getByRole("textbox"), run.confirmation_phrase);
    await user.click(screen.getByRole("button", { name: /^Reap 1 items$/ }));

    await screen.findByText(/1 soul reclaimed/);
    expect(screen.getByText("A Film")).toBeInTheDocument();
    expect(screen.getByText(/Nobody was watching it right now/)).toBeInTheDocument();
  });
});
