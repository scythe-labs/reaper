// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The library scan's start, said out loud.
//
// Pressing "Scan library" disables its own button and swaps the schedule line for a progress
// bar. Both of those are visual, the disable drops focus to `<body>`, and a `role="progressbar"`
// announces nothing by itself -- so for an operation that runs for minutes the next thing an
// operator using a screen reader heard was the finish (#177).
import { QueryClient } from "@tanstack/react-query";
import { act, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { Snapshot } from "../api";
import { expectNoA11yViolations } from "../test/a11y";
import { testQueryClient } from "../test/queryClient";
import { renderWithProviders } from "../test/renderWithProviders";
import { ScanRow } from "./ScanBar";

const { apiMock } = await vi.hoisted(async () => ({
  apiMock: (await import("../test/apiMock")).makeApiMock(),
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

const DEGRADED: Snapshot = {
  id: 7,
  created_at: "2026-01-02T00:00:00+00:00",
  policy_hash: "p",
  horizon_at: "2025-01-01T00:00:00+00:00",
  item_count: 10,
  degraded: true,
  degraded_reason: "Sonarr didn't answer.",
  // The ordinary degradation: a source that did not answer has no page to send anyone to.
  // `offers the help page the scan named` below is the other half.
  degraded_doc: null,
  condemned: 2,
  protected: 3,
  abstained: 5,
  reclaimable_bytes: 0,
  unknown_size_items: 0,
};

/** The row alone. `rerender` re-wraps it in the providers `renderRow` mounted it under, so a
 *  re-render keeps reading the same cache rather than being handed a fresh one. */
function rowTree(snapshot: Snapshot | undefined) {
  return (
    <ScanRow
      snapshot={snapshot}
      scanJob={undefined}
      title="Library scan"
      desc="Reads your library and scores it."
      scheduleText="Every day at 4am"
      onEdit={() => {}}
      canEdit
    />
  );
}

function renderRow(snapshot?: Snapshot, client: QueryClient = testQueryClient()) {
  return renderWithProviders(rowTree(snapshot), { client });
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
        ["Scanning your library. You can leave this page. It keeps running."],
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

  it("holds the last scan's incomplete notice while the next scan runs", async () => {
    // "This scan came back incomplete" renders inside this row, under the bar of the scan in
    // flight, so during a run it names the wrong scan -- and the operator pressing Scan library
    // has already done what it asks. Every other last-scan fact on the row already waits.
    apiMock.scanStatus.mockResolvedValue(RUNNING);
    renderRow(DEGRADED);

    await screen.findByRole("progressbar");
    expect(screen.queryByText(/came back incomplete/i)).not.toBeInTheDocument();
  });

  it("says the last scan was incomplete while nothing is running", async () => {
    // The other half of the same claim: with no scan in flight the snapshot in hand IS the last
    // scan's, and a warning that hides on the idle page would be no warning at all.
    apiMock.scanStatus.mockResolvedValue(IDLE);
    renderRow(DEGRADED);

    expect(await screen.findByText(/came back incomplete/i)).toBeInTheDocument();
    expect(screen.getByText(/sonarr didn't answer/i)).toBeInTheDocument();
    // The control case for the pair below: this degradation named no page, so there is no
    // button. Without it, a link that renders unconditionally would still pass that test.
    expect(screen.queryByRole("button", { name: /rebuilding a plex library/i })).toBeNull();
  });

  it("offers the help page when the scan named one", async () => {
    apiMock.scanStatus.mockResolvedValue(IDLE);
    renderRow({ ...DEGRADED, degraded_doc: "plex-rebuild" });

    // The label is the page's own title, read from the docs registry rather than written here,
    // so this fails if the doc is renamed out from under the id the backend stores.
    expect(
      await screen.findByRole("button", { name: /rebuilding a plex library/i }),
    ).toBeInTheDocument();
  });

  it("offers nothing for a page this build does not have", async () => {
    // A snapshot outlives the version that wrote it, so an id from a newer Reaper can arrive
    // here. A button opening an empty modal is worse than no button.
    apiMock.scanStatus.mockResolvedValue(IDLE);
    renderRow({ ...DEGRADED, degraded_doc: "a-page-from-a-later-release" });

    expect(await screen.findByText(/came back incomplete/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /a-page-from-a-later-release/i })).toBeNull();
  });

  it("keeps holding it between the finish and the fresh snapshot, then speaks for that one", async () => {
    // The row goes idle before the snapshot underneath is replaced: `useScanSettled` invalidates
    // `["snapshot"]` on that edge and the refetch is still out, so what is in hand is the scan
    // before this one. Painting its verdict there reports it as the new scan's (rule 85).
    const client = testQueryClient();
    client.setQueryData(["snapshot"], DEGRADED);
    apiMock.scanStatus.mockResolvedValue(RUNNING);
    const { rerender } = renderRow(DEGRADED, client);
    await screen.findByRole("progressbar");

    // Wait for the row to settle, not just for the write: the two arrive at least a round trip
    // apart in the app, and flipping the status and the snapshot inside one tick lets the row
    // read the new snapshot as the one it started from -- a fixture that pins nothing (rule 141).
    apiMock.scanStatus.mockResolvedValue(IDLE);
    await act(async () => {
      client.setQueryData(["scanStatus"], IDLE);
    });
    await waitFor(() => expect(screen.queryByRole("progressbar")).not.toBeInTheDocument());
    expect(screen.queryByText(/came back incomplete/i)).not.toBeInTheDocument();

    const fresh = { ...DEGRADED, id: 8, item_count: 11, degraded_reason: "Radarr didn't answer." };
    client.setQueryData(["snapshot"], fresh);
    rerender(rowTree(fresh));

    expect(await screen.findByText(/came back incomplete/i)).toBeInTheDocument();
    expect(screen.getByText(/radarr didn't answer/i)).toBeInTheDocument();
  });

  it("puts it back when the refetch that would replace the snapshot fails", async () => {
    // The window above is bounded by the READ settling, not by a new id arriving. `before` is
    // cleared only when the NEXT scan starts, so on id-equality alone a refetch that never
    // lands with a new id held the warning down for the life of the mount -- and JobsPanel's
    // query is `retry: false`, so one dropped request does exactly that. The row went on
    // rendering this snapshot's counts while withholding its verdict, which is the reassuring
    // direction to fail in (rules 17/36, 85).
    const client = testQueryClient();
    client.setQueryData(["snapshot"], DEGRADED);
    apiMock.scanStatus.mockResolvedValue(RUNNING);
    renderRow(DEGRADED, client);
    await screen.findByRole("progressbar");

    apiMock.scanStatus.mockResolvedValue(IDLE);
    await act(async () => {
      client.setQueryData(["scanStatus"], IDLE);
    });
    await waitFor(() => expect(screen.queryByRole("progressbar")).not.toBeInTheDocument());

    // The refetch goes out and FAILS. Held open across a render rather than rejected inside one
    // flush, because that is the shape of a real request: the row has to SEE the read in flight
    // and then see it settle. An error leaves the cached snapshot in place, so the id on screen
    // is still the one `before` holds.
    let dropIt: (e: Error) => void = () => {};
    const inFlight = new Promise<never>((_, reject) => {
      dropIt = reject;
    });
    await act(async () => {
      void client
        .fetchQuery({ queryKey: ["snapshot"], queryFn: () => inFlight, retry: false })
        .catch(() => {});
      await Promise.resolve();
    });
    // Still held: the read has not settled, so the row still cannot speak for this snapshot.
    expect(screen.queryByText(/came back incomplete/i)).not.toBeInTheDocument();

    // Once it has settled, the snapshot in hand IS the newest thing Reaper has, and it is
    // incomplete. Before this, the warning stayed down for the life of the mount.
    await act(async () => {
      dropIt(new Error("dropped"));
      await inFlight.catch(() => {});
    });

    expect(await screen.findByText(/came back incomplete/i)).toBeInTheDocument();
  });

  it("puts it back when the scan ends in an error, having written no snapshot", async () => {
    // Nothing replaced the snapshot, so it is still the newest thing Reaper has and still
    // incomplete. The error notice says what just failed; this one says what the queue is
    // built on, and holding it here would leave that unsaid until the page reloads.
    const client = testQueryClient();
    client.setQueryData(["snapshot"], DEGRADED);
    apiMock.scanStatus.mockResolvedValue(RUNNING);
    renderRow(DEGRADED, client);
    await screen.findByRole("progressbar");

    const failed = { ...IDLE, phase: "error", error: "Sonarr is unreachable" };
    apiMock.scanStatus.mockResolvedValue(failed);
    await act(async () => {
      client.setQueryData(["scanStatus"], failed);
    });

    expect(await screen.findByText(/came back incomplete/i)).toBeInTheDocument();
    expect(screen.getByText(/the scan hit a problem/i)).toBeInTheDocument();
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
