// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The first-run wizard, now four steps rather than one screen.
//
// Two things matter most here and both are pinned below. **Where the operator lands is derived
// from the server**, so closing the tab resumes rather than restarting, and an install that
// never set a password cannot walk past the step that creates the local account. And **once
// the first scan is running you can leave for the app immediately**, instead of being held on
// the wizard until it finishes -- the behavior this file was originally written for.
import { QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { SetupStatus } from "../api";
import { expectNoA11yViolations } from "../test/a11y";
import { testQueryClient } from "../test/queryClient";
import { SetupWizard } from "./SetupWizard";

// The Plex and Connect steps drive the real service and Plex surfaces, which make their own
// queries and are not what these tests are about. Stubbed so each test drives one step.
vi.mock("./SetupPlexStep", () => ({ SetupPlexStep: () => null }));
vi.mock("./SetupConnectStep", () => ({ SetupConnectStep: () => null }));

const { apiMock } = vi.hoisted(() => ({
  apiMock: {
    setupStatus: vi.fn(),
    scanStatus: vi.fn(),
    startScan: vi.fn(),
    setAdminPassword: vi.fn(),
    // The scan step's Discord row reads this. Named even though no test below asserts on it:
    // a mock that omits a read the tree performs hands it `undefined`, and rule 135 fails the
    // run rather than letting the tree render its failed-read branch unnoticed.
    notifications: vi.fn(),
  },
}));
vi.mock("../api", () => ({ api: apiMock }));

const { announceSpy } = vi.hoisted(() => ({ announceSpy: vi.fn() }));
vi.mock("../announce", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../announce")>()),
  announce: announceSpy,
}));

/** Everything done except the scan: the state that lands on the last step. */
const AT_SCAN: SetupStatus = {
  admin_exists: true,
  has_password: true,
  plex_linked: true,
  instances: { radarr: 1, tautulli: 1 },
  has_radarr: true,
  has_sonarr: false,
  has_tautulli: true,
  has_seerr: false,
  has_scanned: false,
  scan_ready: true,
  reap_ready: true,
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
const RUNNING = { ...IDLE, running: true, phase: "history", percent: 3, detail: "syncing" };

function renderWizard(onSkip: () => void = () => {}) {
  return render(
    <QueryClientProvider client={testQueryClient()}>
      <SetupWizard onSkip={onSkip} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  apiMock.notifications.mockResolvedValue({ has_webhook: false });
  apiMock.scanStatus.mockResolvedValue(IDLE);
});

// This screen replaces the whole app shell, so its landmarks are the only ones on the page --
// which is why these audit `document.body` with `pageLevel`, the way `Login` and the shell do,
// rather than a container inside a page that already has a `<main>` above it. Every branch is
// driven: the wizard renders a different tree per step and another when the status cannot be
// read, and the branch a passing default hands you for free is the one a missing landmark
// hides behind (rule 145).
describe("the first screen a new operator meets", () => {
  it("has no accessibility violations on the password step", async () => {
    apiMock.setupStatus.mockResolvedValue({ ...AT_SCAN, has_password: false });
    renderWizard();
    await screen.findByRole("heading", { name: /set your password/i });
    await expectNoA11yViolations(document.body, { pageLevel: true });
  });

  it("has none on the scan step either", async () => {
    apiMock.setupStatus.mockResolvedValue(AT_SCAN);
    renderWizard();
    await screen.findByRole("heading", { name: /run your first scan/i });
    await expectNoA11yViolations(document.body, { pageLevel: true });
  });

  it("has none either when the setup status cannot be read", async () => {
    apiMock.setupStatus.mockRejectedValue(new Error("nope"));
    renderWizard();
    await screen.findByRole("heading", { name: /welcome to reaper/i });
    await expectNoA11yViolations(document.body, { pageLevel: true });
  });
});

describe("where the wizard resumes", () => {
  // The whole point of deriving the step from the server rather than from this browser: an
  // install that never set a password must not be able to walk past the step that creates the
  // local account, however many times the tab is closed and reopened.
  it("opens on the password step when no password is set", async () => {
    apiMock.setupStatus.mockResolvedValue({ ...AT_SCAN, has_password: false });
    renderWizard();
    expect(await screen.findByRole("heading", { name: /set your password/i })).toBeTruthy();
  });

  it("offers no way past the password step", async () => {
    apiMock.setupStatus.mockResolvedValue({ ...AT_SCAN, has_password: false });
    renderWizard();
    await screen.findByRole("heading", { name: /set your password/i });
    // No Skip and no Back: this password is the local account, the arming credential and the
    // restore confirm all at once, so there is nothing here to defer.
    expect(screen.queryByRole("button", { name: /skip/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /^back$/i })).toBeNull();
  });

  it("opens on the scan step once everything before it is done", async () => {
    apiMock.setupStatus.mockResolvedValue(AT_SCAN);
    renderWizard();
    expect(await screen.findByRole("heading", { name: /run your first scan/i })).toBeTruthy();
  });

  it("leaves a failed status read with a way out, rather than trapping the owner", async () => {
    apiMock.setupStatus.mockRejectedValue(new Error("nope"));
    const onSkip = vi.fn();
    renderWizard(onSkip);
    const out = await screen.findByRole("button", { name: /go to the app/i });
    await userEvent.click(out);
    expect(onSkip).toHaveBeenCalled();
  });
});

describe("the password step", () => {
  it("refuses a password shorter than the floor, and says how far along it is", async () => {
    apiMock.setupStatus.mockResolvedValue({ ...AT_SCAN, has_password: false });
    renderWizard();
    const pw = await screen.findByLabelText(/^password/i);
    await userEvent.type(pw, "short");
    expect(screen.getByRole("button", { name: /set password/i })).toHaveProperty("disabled", true);
    // The running count, not the floor: the floor is also in the field's standing help, so
    // matching on it would pass whether or not the complaint ever rendered.
    expect(screen.getByText(/5 so far/i)).toBeTruthy();
  });

  it("refuses two that do not match", async () => {
    apiMock.setupStatus.mockResolvedValue({ ...AT_SCAN, has_password: false });
    renderWizard();
    await userEvent.type(await screen.findByLabelText(/^password/i), "a-long-enough-one");
    await userEvent.type(screen.getByLabelText(/confirm password/i), "a-different-one");
    expect(screen.getByText(/don't match/i)).toBeTruthy();
    expect(screen.getByRole("button", { name: /set password/i })).toHaveProperty("disabled", true);
  });
});

describe("the first scan", () => {
  beforeEach(() => {
    apiMock.setupStatus.mockResolvedValue(AT_SCAN);
  });

  it("offers Run first scan when connected and idle", async () => {
    renderWizard();
    expect(await screen.findByRole("button", { name: /run first scan/i })).toBeTruthy();
  });

  it("lets you head into the app the moment the first scan is running", async () => {
    apiMock.scanStatus.mockResolvedValue(RUNNING);
    const onSkip = vi.fn();
    renderWizard(onSkip);
    // Wait for the RUNNING branch before reaching for its button. The idle branch offers a
    // button of the same name, and resolving on that one clicks a node the next render has
    // already replaced -- the click lands on a detached element and nothing happens (rule 137).
    await screen.findByText(/your first scan is running/i);
    await userEvent.click(screen.getByRole("button", { name: /go to the app/i }));
    expect(onSkip).toHaveBeenCalled();
  });

  it("draws a determinate bar from the scan's own percent", async () => {
    // The number the API returns for exactly this, rather than done/total, whose denominator
    // changes meaning between phases.
    apiMock.scanStatus.mockResolvedValue({ ...RUNNING, percent: 42 });
    renderWizard();
    const bars = await screen.findAllByRole("progressbar", { name: /scanning your library/i });
    // Two surfaces, one number: the bar on the card and the line at the top of the window.
    expect(bars.length).toBe(2);
    for (const bar of bars) {
      expect(bar.getAttribute("aria-valuenow")).toBe("42");
    }
  });
});

describe("the first scan finishing", () => {
  beforeEach(() => {
    apiMock.setupStatus.mockResolvedValue(AT_SCAN);
  });

  it("says so, because nothing the operator did ended it", async () => {
    // The scan status is polled every second while it runs, and the settled panel only appears
    // once that poll has answered AND the invalidation it triggers has refetched the setup
    // read. The clock is driven rather than waited on: a bare `findByText` races its own
    // default 1000ms window against that same 1000ms interval (rule 133).
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      apiMock.scanStatus.mockResolvedValue(RUNNING);
      renderWizard();
      expect(await screen.findByText(/your first scan is running/i)).toBeTruthy();

      apiMock.setupStatus.mockResolvedValue({ ...AT_SCAN, has_scanned: true, complete: true });
      apiMock.scanStatus.mockResolvedValue(IDLE);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2500);
      });

      expect(announceSpy.mock.calls).toEqual([
        ["Your first scan finished. Reaper has scanned your library."],
      ]);
    } finally {
      vi.useRealTimers();
    }
  });

  it("says a crashed first scan stopped, rather than that it finished", async () => {
    // `api/scan.py` clears `running` in a `finally`, so a crashed scan reaches the same
    // running -> not-running edge a clean one does, and the panel behind it does not say
    // "all set" either. An unconditional announcement would contradict the screen.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      apiMock.scanStatus.mockResolvedValue(RUNNING);
      renderWizard();
      expect(await screen.findByText(/your first scan is running/i)).toBeTruthy();

      apiMock.scanStatus.mockResolvedValue({ ...IDLE, error: "Tautulli timed out" });
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
    apiMock.startScan.mockResolvedValue(RUNNING);
    renderWizard();
    await userEvent.click(await screen.findByRole("button", { name: /run first scan/i }));
    expect(announceSpy.mock.calls).toEqual([
      ["Your first scan is running. You don't have to wait here."],
    ]);
  });

  it("stays quiet on a step that mounts already-scanned", async () => {
    apiMock.setupStatus.mockResolvedValue({ ...AT_SCAN, has_scanned: true });
    renderWizard();
    await screen.findByText(/you're all set/i);
    expect(announceSpy).not.toHaveBeenCalled();
  });
});
