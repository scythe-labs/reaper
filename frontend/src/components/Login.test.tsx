// SPDX-License-Identifier: AGPL-3.0-or-later
// The local-account sheet declares aria-modal="true", which promises the page behind it is
// unreachable. It keeps its own markup (it slides up rather than appearing over a scrim),
// so these pin the part it borrows from ModalShell: Tab stays inside the sheet, in both
// directions, instead of landing on the sign-in buttons behind it.
import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { testQueryClient } from "../test/queryClient";
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
  const queryClient = testQueryClient();
  render(
    <QueryClientProvider client={queryClient}>
      <Login />
    </QueryClientProvider>,
  );
  const person = userEvent.setup();
  await person.click(await screen.findByRole("button", { name: /use a local account/i }));
  return person;
}

describe("the local-account sheet", () => {
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
});

describe("the Plex sign-in popup", () => {
  it("opens with noopener, so plex.tv gets no handle on the page that takes the password", async () => {
    // S-4: without it the auth page holds `window.opener` pointing at this window and can
    // navigate it -- and what it would be navigating away from is Reaper's own sign-in.
    apiMock.plexStart.mockResolvedValue({ pin_id: 1, auth_url: "https://plex.test/link" });
    apiMock.plexPoll.mockResolvedValue({ status: "pending" });
    const open = vi.fn<typeof window.open>(() => null);
    vi.stubGlobal("open", open);

    const queryClient = testQueryClient();
    render(
      <QueryClientProvider client={queryClient}>
        <Login />
      </QueryClientProvider>,
    );
    const person = userEvent.setup();
    await person.click(await screen.findByRole("button", { name: /sign in with plex/i }));

    await waitFor(() => expect(open).toHaveBeenCalled());
    expect(open.mock.calls[0]?.[2] ?? "").toContain("noopener");
    vi.unstubAllGlobals();
  });
});

describe("the recovery card", () => {
  it("sends a locked-out operator to the console, which is the only place the code is", async () => {
    // mint_recovery_token prints its banner rather than logging it, deliberately, so the
    // code never reaches the in-app Logs tab or the files that tab downloads. The card used
    // to say "to its log", which sent people to Settings -> Logs to find nothing (U-11).
    window.history.pushState({}, "", "/recover");
    const queryClient = testQueryClient();
    render(
      <QueryClientProvider client={queryClient}>
        <Login />
      </QueryClientProvider>,
    );

    const note = await screen.findByText(/Reaper printed a recovery code/);
    expect(note.textContent).toContain("console output");
    expect(note.textContent).not.toContain("to its log");
    window.history.pushState({}, "", "/");
  });
});
