// SPDX-License-Identifier: AGPL-3.0-or-later
// The local-account sheet declares aria-modal="true", which promises the page behind it is
// unreachable. It keeps its own markup (it slides up rather than appearing over a scrim),
// so these pin the part it borrows from ModalShell: Tab stays inside the sheet, in both
// directions, instead of landing on the sign-in buttons behind it.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
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
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
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
