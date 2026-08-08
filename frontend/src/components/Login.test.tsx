// SPDX-License-Identifier: AGPL-3.0-or-later
// The local-account sheet declares aria-modal="true", which promises the page behind it is
// unreachable. It keeps its own markup (it slides up rather than appearing over a scrim),
// so these pin the part it borrows from ModalShell: Tab stays inside the sheet, in both
// directions, instead of landing on the sign-in buttons behind it.
import { act, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { Announcer } from "../announce";
import { expectNoA11yViolations } from "../test/a11y";
import { renderWithProviders } from "../test/renderWithProviders";
import { Login } from "./Login";

const { apiMock } = vi.hoisted(() => ({
  apiMock: {
    authContext: vi.fn(async () => ({
      setup_needed: false,
      plex_linked: true,
      local_login_available: true,
    })),
    localLogin: vi.fn(),
    plexStart: vi.fn(),
    plexPoll: vi.fn(),
  },
}));

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  api: apiMock,
}));

async function openSheet() {
  renderWithProviders(<Login />);
  const person = userEvent.setup();
  await person.click(await screen.findByRole("button", { name: /use a local account/i }));
  return person;
}

describe("the local-account sheet", () => {
  // This sheet is the only way into Reaper for an operator with no Plex account, and it is the
  // first screen anyone meets. A username or password box a reader cannot name locks them out of
  // the app with nothing to try.
  // `pageLevel`, unlike the panel audits: the sign-in screen IS the whole page rather than a
  // piece of one, so it has to answer for its own landmarks, and the signed-in shell's audit
  // cannot answer for it. It had none at all until `auth-card` became a `main`.
  it("has no accessibility violations", async () => {
    await openSheet();
    await screen.findByLabelText(/username/i);
    await expectNoA11yViolations(document.body, { pageLevel: true });
  });

  it("wraps Tab from the last control back to the first", async () => {
    const person = await openSheet();
    const username = screen.getByLabelText(/username/i);
    const password = screen.getByLabelText(/password/i);
    await person.type(username, "someone");
    await person.type(password, "a-password");

    const signIn = screen.getByRole("button", { name: /^sign in$/i });
    signIn.focus();
    await person.tab();

    expect(document.activeElement).toBe(username);
  });

  it("wraps Shift+Tab from the first control back to the last", async () => {
    const person = await openSheet();
    const username = screen.getByLabelText(/username/i);
    username.focus();
    await person.tab({ shift: true });

    // Sign in is disabled while the form is empty, so the last thing a browser would put in
    // the Tab order is Back. Either way it is inside the sheet.
    expect(screen.getByRole("button", { name: /back/i })).toBe(document.activeElement);
  });

  it("keeps focus inside the sheet however long you hold Tab", async () => {
    const person = await openSheet();
    const sheet = screen.getByRole("dialog");
    screen.getByLabelText(/username/i).focus();
    for (let i = 0; i < 8; i++) {
      await person.tab();
      expect(sheet.contains(document.activeElement)).toBe(true);
    }
  });

  // #382: the sheet used to assert the safeguard flat, so on an install that had no local
  // account the promise and its own denial rendered one above the other. The claim now
  // renders only where it is true, which is what these two pin -- from opposite directions,
  // because a claim narrowed to nowhere is the same failure the other way round.
  it("promises the fallback on an install that actually has one", async () => {
    await openSheet();
    const blurb = await screen.findByText(/fallback for when plex is unreachable/i);
    expect(blurb).toHaveTextContent(/keeps at least one/i);
  });

  it("does not promise the fallback on an install with no local account", async () => {
    apiMock.authContext.mockResolvedValue({
      setup_needed: false,
      plex_linked: true,
      local_login_available: false,
    });
    try {
      await openSheet();
      const blurb = await screen.findByText(/fallback for when plex is unreachable/i);
      expect(blurb).toHaveTextContent(/doesn’t have one yet/i);
      expect(blurb).not.toHaveTextContent(/keeps at least one/i);
      // And the way out is the one reachable from this browser. The host command stays for
      // an operator who cannot reach plex.tv, which is the case they opened this sheet in.
      //
      // Found by its words rather than by `role="alert"`, which it no longer carries: whether
      // this install has a local account is read from `["authContext"]` on first load, so the
      // notice is on the sheet before anything is pressed and is `standing` (#394). The sheet is
      // always mounted and only translated off-screen, so the press that opens it reveals a node
      // that was already there. Asserted rather than merely worked around, so putting the alert
      // back fails here.
      const notice = screen.getByText(/sign in with plex above/i).closest(".notice");
      expect(notice).not.toHaveAttribute("role", "alert");
      expect(notice).toHaveTextContent(/reaper-admin create-admin/i);
    } finally {
      apiMock.authContext.mockResolvedValue({
        setup_needed: false,
        plex_linked: true,
        local_login_available: true,
      });
    }
  });
});

describe("the Plex sign-in popup", () => {
  it("opens with noopener, so plex.tv gets no handle on the page that takes the password", async () => {
    // S-4: without it the auth page holds `window.opener` pointing at this window and can
    // navigate it -- and what it would be navigating away from is Reaper's own sign-in.
    apiMock.plexStart.mockResolvedValue({ pin_id: 1, auth_url: "https://plex.test/link" });
    apiMock.plexPoll.mockResolvedValue({ status: "pending" });
    const open = vi.fn<typeof window.open>(() => null);
    vi.stubGlobal("open", open);

    renderWithProviders(<Login />);
    const person = userEvent.setup();
    await person.click(await screen.findByRole("button", { name: /sign in with plex/i }));

    await waitFor(() => expect(open).toHaveBeenCalled());
    expect(open.mock.calls[0]?.[2] ?? "").toContain("noopener");
    vi.unstubAllGlobals();
  });

  it("says so when the wait turns into a list of servers to choose from", async () => {
    // The picker replaces the "Waiting for Plex" block on a two-second poll rather than on a
    // press, so the screen grew a list of servers with nothing to say it had (#177). The
    // Settings link panel already announced this and its twin here did not, which is the whole
    // shape of rule 72: one of a pair fixed, the other left.
    apiMock.plexStart.mockResolvedValue({ pin_id: 1, auth_url: "https://plex.test/link" });
    apiMock.plexPoll.mockResolvedValue({
      status: "choose_server",
      servers: [{ machine_identifier: "m-1", name: "The one downstairs" }],
    });
    vi.stubGlobal(
      "open",
      vi.fn<typeof window.open>(() => null),
    );
    // The first poll is a 2s tick away, which is past every default timeout in here. Rule 133:
    // restored in the `finally`, or the next test in the file inherits the fake clock.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      renderWithProviders(
        <>
          {/* The region `announce()` speaks through. Without it the call is a no-op by design,
              so this test would pass against a component that says nothing. */}
          <Announcer />
          <Login />
        </>,
      );
      const person = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
      await person.click(await screen.findByRole("button", { name: /sign in with plex/i }));
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2000);
      });

      // The picker is on screen...
      await screen.findByRole("button", { name: "The one downstairs" });
      // ...and it was said out loud, in one of the two regions the announcer alternates between.
      await waitFor(() =>
        expect(
          screen
            .getAllByRole("status")
            .map((n) => n.textContent)
            .join(""),
        ).toContain("Choose which server Reaper should manage"),
      );
    } finally {
      vi.useRealTimers();
      vi.unstubAllGlobals();
    }
  });
});

describe("the recovery card", () => {
  it("names both places the code is, and the log is not one of them", async () => {
    // mint_recovery_token prints its banner rather than logging it, deliberately, so the
    // code never reaches the in-app Logs tab or the files that tab downloads. The card used
    // to say "to its log", which sent people to Settings -> Logs to find nothing (U-11).
    //
    // The file is the half this test was missing: the console is not a channel at all on a
    // windowed Windows build or a Finder-launched .app, so naming it alone left the operators
    // who most need this screen with nowhere to look (#433). Both are asserted, because a card
    // naming one of two is exactly as wrong for half the install base as naming neither.
    window.history.pushState({}, "", "/recover");
    renderWithProviders(<Login />);

    const note = await screen.findByText(/The code is in/);
    expect(note.textContent).toContain("recovery.txt");
    expect(note.textContent).toContain("console output");
    expect(note.textContent).not.toContain("to its log");
    // The promise this screen makes about where it lands the operator. It was false until the
    // session carried a recovery mark, so it is pinned rather than left to read as decoration.
    expect(note.textContent).toContain("without the old one");
    window.history.pushState({}, "", "/");
  });

  it("has no accessibility violations, landmarks included", async () => {
    // The twin of the sign-in card's `main`, and the half that nothing pinned: reverting only
    // this one back to a `div` left every test in this file green. A locked-out operator
    // reaches this screen and no other, so its landmarks are the only ones on the page and the
    // sheet audit above cannot answer for them (rule 72, rule 118).
    window.history.pushState({}, "", "/recover");
    renderWithProviders(<Login />);

    await screen.findByText(/The code is in/);
    await expectNoA11yViolations(document.body, { pageLevel: true });
    window.history.pushState({}, "", "/");
  });
});
