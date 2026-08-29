// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The first-run wizard has four steps.
//
// Two things matter most here, and both are pinned below. Where the operator lands is derived
// from the server, so closing the tab resumes instead of restarting, and an install that never
// set a password cannot walk past the step that creates the local account. And once the first
// scan is running, you can leave for the app immediately, instead of being held on the wizard
// until it finishes.
import { act, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { SetupStatus } from "../api";
import { expectNoA11yViolations } from "../test/a11y";
import { testQueryClient } from "../test/queryClient";
import { renderWithProviders } from "../test/renderWithProviders";
import { SetupWizard } from "./SetupWizard";

// The Plex and Connect steps drive the real service and Plex surfaces, which make their own
// queries and are not what these tests are about. They are stubbed so each test drives one
// step.
//
// Both stubs render, rather than returning null. The Connect stub says the password it was
// handed, because the wizard holds what was typed on step one so the restore door does not ask
// for it again, and a stub returning null would leave `password={password}` pinned by nothing.
// Passing `null` there would keep every test in this file passing while putting the password
// box back in front of the operator. The Plex stub carries a Next, because reaching Connect
// with a password in state means walking there from step one, and a stub with no control
// cannot be walked through.
vi.mock("./SetupPlexStep", () => ({
  SetupPlexStep: ({ onNext }: { onNext: () => void }) => (
    <button type="button" onClick={onNext}>
      stub: on to Connect
    </button>
  ),
}));
vi.mock("./SetupConnectStep", () => ({
  SetupConnectStep: ({ password }: { password: string | null }) => (
    <p>{password === null ? "stub: holding no password" : `stub: holding ${password}`}</p>
  ),
}));

const { apiMock } = await vi.hoisted(async () => ({
  apiMock: (await import("../test/apiMock")).makeApiMock(),
}));
vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  api: apiMock,
}));

const { announceSpy } = vi.hoisted(() => ({ announceSpy: vi.fn() }));
vi.mock("../announce", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../announce")>()),
  announce: announceSpy,
}));

/** The state that lands on the last step, with everything done except the scan. */
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
  detail_reason: null,
  error_reason: null,
  snapshot_id: null,
  followup_queued: false,
};
const RUNNING = {
  ...IDLE,
  running: true,
  phase: "history",
  percent: 3,
  detail_reason: { k: "history_sync", p: null },
};

function renderWizard(onSkip: () => void = () => {}) {
  return renderWithProviders(<SetupWizard onSkip={onSkip} />);
}

beforeEach(() => {
  vi.clearAllMocks();
  apiMock.notifications.mockResolvedValue({ has_webhook: false });
  apiMock.scanStatus.mockResolvedValue(IDLE);
  apiMock.safety.mockResolvedValue({
    destructive_enabled: false,
    has_password: true,
  });
});

// This screen replaces the whole app shell, so its landmarks are the only ones on the page.
// That is why these audit `document.body` with `pageLevel`, the way `Login` and the shell do,
// rather than a container inside a page that already has a `<main>` above it. Every branch is
// driven, since the wizard renders a different tree per step and another when the status
// cannot be read. The branch a passing default hands you for free is the one a missing
// landmark hides behind.
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
  // The step comes from the server rather than from this browser, because an install that
  // never set a password must not be able to walk past the step that creates the local
  // account, however many times the tab is closed and reopened.
  it("opens on the password step when no password is set", async () => {
    apiMock.setupStatus.mockResolvedValue({ ...AT_SCAN, has_password: false });
    renderWizard();
    expect(await screen.findByRole("heading", { name: /set your password/i })).toBeTruthy();
  });

  it("offers no way past the password step", async () => {
    apiMock.setupStatus.mockResolvedValue({ ...AT_SCAN, has_password: false });
    renderWizard();
    await screen.findByRole("heading", { name: /set your password/i });
    // No Skip and no Back. This password is the local account, the arming credential, and the
    // restore confirm all at once, so there is nothing here to defer.
    expect(screen.queryByRole("button", { name: /skip/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /^back$/i })).toBeNull();
  });

  it("carries the password typed on step one through to the Connect step", async () => {
    // The restore door sits on Connect, and `restore/confirm` refuses outright without an
    // admin password, so the wizard holds what was typed on step one and the door confirms
    // with it. That hand-off is a single prop three steps apart, and nothing here saw it
    // directly: both intervening steps are stubbed, so this walks the operator's own route to
    // it rather than handing the prop straight to a rendered stub.
    const person = userEvent.setup();
    apiMock.setupStatus.mockResolvedValue({ ...AT_SCAN, has_password: false });
    apiMock.setAdminPassword.mockResolvedValue({ ok: true });
    renderWizard();
    await screen.findByRole("heading", { name: /set your password/i });
    // This match is anchored, not exact, since the label's accessible name carries its help
    // text too, and "Confirm password" is the only other match a loose one would find.
    await person.type(screen.getByLabelText(/^password/i), "a-typed-password");
    await person.type(screen.getByLabelText(/confirm password/i), "a-typed-password");

    await person.click(screen.getByRole("button", { name: /set password and continue/i }));
    await person.click(await screen.findByRole("button", { name: /stub: on to connect/i }));

    expect(await screen.findByText("stub: holding a-typed-password")).toBeInTheDocument();
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
    // This checks the running count, not the floor, since the floor is also in the field's
    // permanent help text, so matching on it would pass whether or not the complaint ever
    // rendered.
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
    // This waits for the RUNNING branch before reaching for its button. The idle branch
    // offers a button of the same name, so resolving on that one clicks a node the next
    // render has already replaced. The click then lands on a detached element and nothing
    // happens.
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
    // Two surfaces show this same number, the bar on the card and the line at the top of the
    // window.
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
    // once that poll has answered and the invalidation it triggers has refetched the setup
    // read. The clock is driven rather than waited on, so the delay is asked for instead of
    // sampled from a real timer. The wait's window is 5000ms (`src/test/setup.ts`), well past
    // the 1000ms poll interval, so there is no race between them.
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

      apiMock.scanStatus.mockResolvedValue({
        ...IDLE,
        error_reason: {
          k: "error.scan.source_unreachable",
          p: { error: "Tautulli timed out" },
        },
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

describe("what the wizard says about deletion", () => {
  // `REAPER_DESTRUCTIVE_ACTIONS_ENABLED=true` can boot a host already armed with `complete`
  // still false, so the wizard is what an operator lands on, and `SafetyBanner` renders inside
  // `Dashboard`, nowhere in setup. So the wizard has to state the current deletion setting
  // itself, correctly in both directions, since a claim narrowed to nowhere fails the same way
  // as a wrong one.
  it("says deletion is on when the host is armed", async () => {
    apiMock.setupStatus.mockResolvedValue(AT_SCAN);
    apiMock.safety.mockResolvedValue({
      destructive_enabled: true,
      has_password: true,
    });
    renderWizard();

    expect(await screen.findByText(/deletion is on/i)).toBeTruthy();
    expect(screen.queryByText(/can look but can't remove anything/i)).toBeNull();
  });

  it("says read-only when it is not", async () => {
    apiMock.setupStatus.mockResolvedValue(AT_SCAN);
    renderWizard();

    expect(await screen.findByText(/can look but can't remove anything/i)).toBeTruthy();
    expect(screen.queryByText(/deletion is on/i)).toBeNull();
  });

  it("says the state is unknown rather than safe when it cannot be read", async () => {
    apiMock.setupStatus.mockResolvedValue(AT_SCAN);
    apiMock.safety.mockRejectedValue(new Error("nope"));
    renderWizard();

    expect(await screen.findByText(/safety state unknown/i)).toBeTruthy();
    expect(screen.queryByText(/can look but can't remove anything/i)).toBeNull();
  });
});

describe("a status that changes under the operator", () => {
  // Every step invalidates ["setup"] when its own work lands. The step must not be re-derived
  // from the answer on every render while nothing has been pressed, or linking Plex would move
  // the operator off the Plex step before its Libraries picker has rendered, and saving the
  // last service would move them off Connect before they have seen the restore door.
  it("does not move the operator forward when the server's answer changes", async () => {
    const client = testQueryClient();
    const AT_CONNECT = { ...AT_SCAN, scan_ready: false, reap_ready: false };
    apiMock.setupStatus.mockResolvedValue(AT_CONNECT);
    renderWithProviders(<SetupWizard onSkip={() => {}} />, { client });
    // The Connect step is stubbed in this file, so "still on Connect" is read as "the scan
    // step has not taken the screen," which is exactly the jump this test pins against.
    await screen.findByRole("list", { name: /setup steps/i }).catch(() => null);
    expect(screen.queryByText(/run your first scan/i)).toBeNull();

    apiMock.setupStatus.mockResolvedValue({ ...AT_CONNECT, scan_ready: true, reap_ready: true });
    await act(async () => {
      await client.invalidateQueries({ queryKey: ["setup"] });
    });

    expect(screen.queryByText(/run your first scan/i)).toBeNull();
  });
});

describe("moving between steps", () => {
  // Each step is its own element, so a move unmounts one card and mounts the next, and the
  // button that was pressed goes with it, dropping focus to <body> unless something takes it.
  // The heading takes focus instead, so a screen reader states which step it landed on.
  // The move itself belongs to `StepCard`, and `SetupStepper.test.tsx` drives it: the steps
  // either side of a move are stubbed in this file, so a heading landing after a move is not
  // observable here. What this file proves instead is that arriving is not moving. A fresh
  // load must not steal focus from a page the operator has not read yet, which is why the
  // card asks whether they pressed anything.
  it("leaves focus alone on the step the wizard opens on", async () => {
    apiMock.setupStatus.mockResolvedValue({ ...AT_SCAN, has_scanned: false });
    renderWizard();

    await screen.findByRole("button", { name: /run first scan/i });
    expect(document.activeElement).toBe(document.body);
  });
});
