// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The one behavior that matters on the first-run screen: once the first scan is running you
// can leave for the app immediately, instead of being held on the wizard until it finishes.
import { QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { expectNoA11yViolations } from "../test/a11y";
import { testQueryClient } from "../test/queryClient";
import { SetupWizard } from "./SetupWizard";

// The wizard embeds the services and Plex panels; they make their own queries and are not
// what these tests are about, so stub them out.
vi.mock("./Settings", () => ({ PlexPanel: () => null, ServicesPanel: () => null }));

const { apiMock } = vi.hoisted(() => ({
  apiMock: { setupStatus: vi.fn(), scanStatus: vi.fn(), startScan: vi.fn() },
}));
vi.mock("../api", () => ({ api: apiMock }));

const READY = {
  has_radarr: true,
  has_sonarr: false,
  has_tautulli: true,
  has_seerr: false,
  has_scanned: false,
  scan_ready: true,
  complete: false,
};
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
const RUNNING = {
  ...IDLE,
  running: true,
  phase: "history",
  percent: 3,
  detail: "syncing watch history",
};

function renderWizard(onSkip: () => void = () => {}) {
  const qc = testQueryClient();
  return render(
    <QueryClientProvider client={qc}>
      <SetupWizard onSkip={onSkip} />
    </QueryClientProvider>,
  );
}

// This screen replaces the whole app shell, so its landmarks are the only ones on the page --
// which is why these audit `document.body` with `pageLevel`, the way `Login` and the shell do,
// rather than a container inside a page that already has a `<main>` above it. Both branches are
// driven: the wizard renders a different tree when the setup status has not been read, and the
// branch a passing default hands you for free is the one a missing landmark hides behind
// (rule 145).
describe("the first screen a new operator meets", () => {
  it("has no accessibility violations, landmarks included", async () => {
    apiMock.setupStatus.mockResolvedValue(READY);
    apiMock.scanStatus.mockResolvedValue(IDLE);
    renderWizard();
    await screen.findByRole("button", { name: /run first scan/i });
    await expectNoA11yViolations(document.body, { pageLevel: true });
  });

  it("has none either when the setup status cannot be read", async () => {
    // App.tsx routes an unreadable status here on the promise that Skip still works, so this
    // branch is reachable on an install that is not fresh at all.
    apiMock.setupStatus.mockRejectedValue(new Error("unreachable"));
    apiMock.scanStatus.mockResolvedValue(IDLE);
    renderWizard();
    await screen.findByText(/couldn't check the setup state/i);
    await expectNoA11yViolations(document.body, { pageLevel: true });
  });
});

describe("SetupWizard first scan", () => {
  it("offers Run first scan when connected and idle, with no way out yet", async () => {
    apiMock.setupStatus.mockResolvedValue(READY);
    apiMock.scanStatus.mockResolvedValue(IDLE);
    renderWizard();
    expect(await screen.findByRole("button", { name: /run first scan/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /go to the app/i })).not.toBeInTheDocument();
  });

  it("lets you head into the app the moment the first scan is running", async () => {
    apiMock.setupStatus.mockResolvedValue(READY);
    apiMock.scanStatus.mockResolvedValue(RUNNING);
    const onSkip = vi.fn();
    const person = userEvent.setup();
    renderWizard(onSkip);

    expect(await screen.findByText(/your first scan is running/i)).toBeInTheDocument();
    // No longer trapped: the run button is gone and a way into the app is offered instead.
    expect(screen.queryByRole("button", { name: /run first scan/i })).not.toBeInTheDocument();
    await person.click(screen.getByRole("button", { name: /go to the app/i }));
    expect(onSkip).toHaveBeenCalledTimes(1);
  });
});

// The first scan ends on a one-second poll rather than on anything the operator did, and it
// changes the whole panel when it does -- the heading, the paragraph and the button's label.
// None of it was announced, so the one screen a new operator cannot skip past finished its one
// long operation in silence (#177).
const { announceSpy } = vi.hoisted(() => ({ announceSpy: vi.fn() }));
vi.mock("../announce", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../announce")>()),
  announce: announceSpy,
}));

describe("the first scan finishing", () => {
  beforeEach(() => {
    announceSpy.mockClear();
  });

  it("says so, because nothing the operator did ended it", async () => {
    // The scan status is polled every second while it runs, and the settled panel only appears
    // once that poll has answered AND the invalidation it triggers has refetched the setup
    // read. Written first as a bare `findByText`, which raced its own default 1000ms window
    // against that same 1000ms interval: it passed at 1013ms on one run and failed outright
    // after a rebase moved the timings. So the clock is driven here rather than waited on --
    // rule 133, a test may not rest on a wall clock production also samples.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      apiMock.setupStatus.mockResolvedValue(READY);
      apiMock.scanStatus.mockResolvedValue(RUNNING);
      renderWizard();
      expect(await screen.findByText(/your first scan is running/i)).toBeInTheDocument();

      // The scan lands. Both reads answer the settled state from here on.
      apiMock.setupStatus.mockResolvedValue({ ...READY, has_scanned: true, complete: true });
      apiMock.scanStatus.mockResolvedValue(IDLE);

      // Past the one-second poll, and past the refetch its invalidation starts.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2500);
      });

      expect(screen.getByText(/you're all set/i)).toBeInTheDocument();
      expect(announceSpy.mock.calls).toEqual([
        ["Your first scan finished. Reaper has scanned your library."],
      ]);
    } finally {
      vi.useRealTimers();
    }
  });

  it("says a crashed first scan stopped, rather than that it finished", async () => {
    // `api/scan.py` clears `running` in a `finally`, so a scan that crashed reaches the same
    // running -> not-running edge a successful one does. The announcement was unconditional, so
    // it told a new operator their library had been scanned while the panel behind it fell back
    // to "Ready to scan" with an error notice beside it -- the sentence and the screen
    // disagreeing about the same event (#177, rule 72 with `useScanSettled`).
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      apiMock.setupStatus.mockResolvedValue(READY);
      apiMock.scanStatus.mockResolvedValue(RUNNING);
      renderWizard();
      expect(await screen.findByText(/your first scan is running/i)).toBeInTheDocument();

      // The scan dies. `running` goes false carrying the reason, and no snapshot was written,
      // so `has_scanned` stays false.
      apiMock.scanStatus.mockResolvedValue({
        ...IDLE,
        phase: "error",
        error: "Radarr refused the connection",
      });

      await act(async () => {
        await vi.advanceTimersByTimeAsync(2500);
      });

      expect(announceSpy.mock.calls).toEqual([
        ["Your first scan stopped before it finished. You can start it again."],
      ]);
    } finally {
      vi.useRealTimers();
    }
  });

  it("says a scan started, because the press swaps the panel and nothing else says so", async () => {
    // "Ready to scan" and its button become a spinner, a heading and a paragraph. That is the
    // whole signal, it is visual, and the copy itself warns the wait "can take a while" -- so
    // the next thing an operator using a reader would have heard was the finish (#177).
    const person = userEvent.setup();
    apiMock.setupStatus.mockResolvedValue(READY);
    apiMock.scanStatus.mockResolvedValue(IDLE);
    apiMock.startScan.mockResolvedValue(RUNNING);
    renderWizard();

    const run = await screen.findByRole("button", { name: /run first scan/i });
    await person.click(run);

    expect(announceSpy.mock.calls).toEqual([
      ["Your first scan is running. You don't have to wait here."],
    ]);
  });

  it("stays quiet on a panel that mounts already-scanned", async () => {
    // The edge trigger's other half: arriving at a finished scan is not news, and a wizard
    // re-rendered by any of its sibling panels must not re-announce a scan that ended before
    // the operator got here.
    apiMock.setupStatus.mockResolvedValue({ ...READY, has_scanned: true, complete: true });
    apiMock.scanStatus.mockResolvedValue(IDLE);
    renderWizard();
    expect(await screen.findByText(/you're all set/i)).toBeInTheDocument();

    expect(announceSpy).not.toHaveBeenCalled();
  });
});
