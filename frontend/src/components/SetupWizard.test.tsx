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
//
// Both stubs render, rather than returning null. The Connect one SAYS the password it was
// handed, because that prop is the whole of #385's "the password is typed once" -- the wizard
// holds what was typed on step one so the restore door does not ask for it again -- and a stub
// returning null left `password={password}` pinned by nothing: passing `null` there would have
// kept every test in this file green while putting the box back in front of the operator
// (rule 141). The Plex one carries a Next, because reaching Connect with a password in state
// means walking there from step one, and a stub with no control cannot be walked through.
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
    // The scan step states the safety regime through the shared `SafetyBanner`, which reads
    // this. It used to hand-write "Deletion is off" and consult nothing, which is how a host
    // armed by env var was told the opposite of the truth on the only screen saying anything.
    safety: vi.fn(),
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
  apiMock.safety.mockResolvedValue({
    destructive_enabled: false,
    has_password: true,
    note: null,
  });
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

  it("carries the password typed on step one through to the Connect step", async () => {
    // #385's "the password is typed once". The restore door sits on Connect, and
    // `restore/confirm` refuses outright without an admin password, so the wizard holds what
    // was typed on step one and the door confirms with it. That hand-off is a single prop
    // three steps apart, and nothing here saw it: both intervening steps are stubbed, so this
    // walks the operator's own route to it rather than handing the prop to a rendered stub.
    const person = userEvent.setup();
    apiMock.setupStatus.mockResolvedValue({ ...AT_SCAN, has_password: false });
    apiMock.setAdminPassword.mockResolvedValue({ ok: true });
    renderWizard();
    await screen.findByRole("heading", { name: /set your password/i });
    // Anchored, not exact: this label's accessible name carries its help text too, and
    // "Confirm password" is the only other match a loose one would find.
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

describe("what the wizard says about deletion", () => {
  // The step used to state this itself, in the green "read-only" tone, reading no query at all:
  // `REAPER_DESTRUCTIVE_ACTIONS_ENABLED=true` boots a host armed with `complete` still false, so
  // the wizard is what it lands on, and `SafetyBanner` renders inside `Dashboard` and therefore
  // nowhere in setup. The one sentence about deletion on screen was wrong, in the reassuring
  // direction. Both regimes are driven, because a claim narrowed to nowhere fails the same way.
  it("says deletion is on when the host is armed", async () => {
    apiMock.setupStatus.mockResolvedValue(AT_SCAN);
    apiMock.safety.mockResolvedValue({
      destructive_enabled: true,
      has_password: true,
      note: null,
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
  // Every step invalidates ["setup"] when its own work lands, and the step used to be re-derived
  // from the answer on every render while nothing had been pressed. So linking Plex moved them
  // off the Plex step before its Libraries picker had rendered, and saving the last service
  // moved them off Connect before they had seen the restore door. Both are one line apart.
  it("does not move the operator forward when the server's answer changes", async () => {
    const client = testQueryClient();
    const AT_CONNECT = { ...AT_SCAN, scan_ready: false, reap_ready: false };
    apiMock.setupStatus.mockResolvedValue(AT_CONNECT);
    render(
      <QueryClientProvider client={client}>
        <SetupWizard onSkip={() => {}} />
      </QueryClientProvider>,
    );
    // The Connect step is stubbed in this file, so "still on Connect" is read as "the scan step
    // has not taken the screen" -- which is exactly the jump being pinned against.
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
  // Each step is its own element, so a move unmounts one card and mounts the next -- and the
  // button that was pressed goes with it, dropping focus to <body>. Nothing announced the new
  // step either, so a screen reader was left at the top of the document with no statement of
  // where it now was. The heading takes focus instead, which names the step as it lands.
  // The move itself is `StepCard`'s, and `SetupStepper.test.tsx` drives it: the steps either
  // side of a move are stubbed in this file, so a heading landing after one is not observable
  // here. What IS this file's to prove is the other half -- arriving is not moving. A fresh load must not steal focus from a page the
  // operator has not read yet, which is why the card asks whether they pressed anything.
  it("leaves focus alone on the step the wizard opens on", async () => {
    apiMock.setupStatus.mockResolvedValue({ ...AT_SCAN, has_scanned: false });
    renderWizard();

    await screen.findByRole("button", { name: /run first scan/i });
    expect(document.activeElement).toBe(document.body);
  });
});
