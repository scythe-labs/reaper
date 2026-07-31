// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The library scan's start, said out loud.
//
// Pressing "Scan library" disables its own button and swaps the schedule line for a progress
// bar. Both of those are visual, the disable drops focus to `<body>`, and a `role="progressbar"`
// announces nothing by itself -- so for an operation that runs for minutes the next thing an
// operator using a screen reader heard was the finish (#177).
import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { expectNoA11yViolations } from "../test/a11y";
import { testQueryClient } from "../test/queryClient";
import { ScanRow } from "./ScanBar";

const { apiMock } = vi.hoisted(() => ({
  apiMock: { scanStatus: vi.fn(), startScan: vi.fn() },
}));
vi.mock("../api", () => ({ api: apiMock }));

const { announceSpy } = vi.hoisted(() => ({ announceSpy: vi.fn() }));
vi.mock("../announce", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../announce")>()),
  announce: announceSpy,
}));

const IDLE = {
  running: false,
  phase: "idle",
  done: 0,
  total: 0,
  percent: 0,
  detail: "",
  error: null,
  snapshot_id: null,
  followup_queued: false,
};
const RUNNING = { ...IDLE, running: true, phase: "history", percent: 4, detail: "" };

function renderRow() {
  return render(
    <QueryClientProvider client={testQueryClient()}>
      <ScanRow
        snapshot={undefined}
        scanJob={undefined}
        title="Library scan"
        desc="Reads your library and scores it."
        scheduleText="Every day at 4am"
        onEdit={() => {}}
        canEdit
      />
    </QueryClientProvider>,
  );
}

describe("starting a library scan", () => {
  beforeEach(() => {
    announceSpy.mockClear();
  });

  it("says the scan started, and that leaving the page does not stop it", async () => {
    // The permission to walk away is the useful half for a wait this long, and it was on screen
    // only. Said from `onSuccess`, so it reports a scan the server actually accepted rather than
    // one still being asked for (rule 85).
    const person = userEvent.setup();
    apiMock.scanStatus.mockResolvedValue(IDLE);
    apiMock.startScan.mockResolvedValue(RUNNING);
    renderRow();

    // Wait for the control, not for the page: the button is disabled while the status read is in
    // flight, and user-event reports a click on a disabled control as success (rule 137).
    const scan = await screen.findByRole("button", { name: /scan library/i });
    await waitFor(() => expect(scan).toBeEnabled());
    await person.click(scan);

    await waitFor(() =>
      expect(announceSpy.mock.calls).toEqual([
        ["Scanning your library. You can leave this page; it keeps running."],
      ]),
    );
  });

  it("says nothing when the start itself fails", async () => {
    // The failure already speaks: it renders through `Notice`, which owns `role="alert"`. A
    // "scanning" sentence here as well would be a second, contradicting announcement about the
    // same press (rule 85).
    const person = userEvent.setup();
    apiMock.scanStatus.mockResolvedValue(IDLE);
    apiMock.startScan.mockRejectedValue(new Error("Sonarr is unreachable"));
    renderRow();

    const scan = await screen.findByRole("button", { name: /scan library/i });
    await waitFor(() => expect(scan).toBeEnabled());
    await person.click(scan);

    expect(await screen.findByText(/the scan didn't start/i)).toBeInTheDocument();
    expect(announceSpy).not.toHaveBeenCalled();
  });

  it("has no accessibility violations while it runs", async () => {
    // The running state is the one worth auditing: it is the state the row spends minutes in,
    // and it is the one that swaps a schedule line for a progress bar and disables the control
    // the operator pressed.
    apiMock.scanStatus.mockResolvedValue(RUNNING);
    const { container } = renderRow();

    await screen.findByRole("progressbar");
    await expectNoA11yViolations(container);
  });

  it("gives the running bar a name and a value a reader can land on", async () => {
    // The bar is the only thing on screen while the scan runs, so it carries the wait's name --
    // the same string the start announcement leads with (rule 144).
    apiMock.scanStatus.mockResolvedValue(RUNNING);
    renderRow();

    const bar = await screen.findByRole("progressbar", { name: /scanning your library/i });
    expect(bar).toHaveAttribute("aria-valuenow", "4");
  });
});
