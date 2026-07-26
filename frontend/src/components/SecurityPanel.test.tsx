// SPDX-License-Identifier: AGPL-3.0-or-later
// The admin password arms deletion and is the anti-lockout fallback, so the form must not let
// a typo through: it confirms the new password, and it says out loud why Save is off (too
// short, with a live count; or the two entries disagree) instead of a silently gray button.
import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { testQueryClient } from "../test/queryClient";
import { SecurityPanel } from "./Settings";

const { apiMock } = vi.hoisted(() => ({
  apiMock: {
    safety: vi.fn(),
    setAdminPassword: vi.fn(async () => ({ ok: true })),
  },
}));

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  api: apiMock,
}));

function renderPanel() {
  const queryClient = testQueryClient();
  render(
    <QueryClientProvider client={queryClient}>
      <SecurityPanel />
    </QueryClientProvider>,
  );
  return userEvent.setup();
}

describe("the admin password form", () => {
  beforeEach(() => {
    apiMock.safety.mockReset();
    apiMock.setAdminPassword.mockReset();
    apiMock.setAdminPassword.mockResolvedValue({ ok: true });
  });

  it("says the password is too short, with a live count, and keeps Save off", async () => {
    apiMock.safety.mockResolvedValue({ has_password: false });
    const person = renderPanel();

    const next = await screen.findByLabelText(/^new password$/i);
    await person.type(next, "hunter7");

    expect(screen.getByText(/use at least 12 characters/i)).toBeInTheDocument();
    expect(screen.getByText(/7 so far/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^save$/i })).toBeDisabled();
    expect(apiMock.setAdminPassword).not.toHaveBeenCalled();
  });

  it("blocks Save until the confirmation matches, then sends only the new password", async () => {
    apiMock.safety.mockResolvedValue({ has_password: false });
    const person = renderPanel();

    const next = await screen.findByLabelText(/^new password$/i);
    const confirm = screen.getByLabelText(/confirm new password/i);
    await person.type(next, "a-long-enough-password");
    await person.type(confirm, "a-long-enough-passwerd"); // typo

    expect(screen.getByText(/the passwords don't match/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^save$/i })).toBeDisabled();

    // Fix the typo -> Save enables and only the new password is sent.
    await person.clear(confirm);
    await person.type(confirm, "a-long-enough-password");
    const save = screen.getByRole("button", { name: /^save$/i });
    expect(save).toBeEnabled();
    await person.click(save);

    expect(apiMock.setAdminPassword).toHaveBeenCalledTimes(1);
    expect(apiMock.setAdminPassword).toHaveBeenCalledWith("a-long-enough-password", undefined);
    expect(await screen.findByText(/password saved/i)).toBeInTheDocument();
  });

  it("needs the current password before Save when one is already set", async () => {
    apiMock.safety.mockResolvedValue({ has_password: true });
    const person = renderPanel();

    const next = await screen.findByLabelText(/^new password$/i);
    const confirm = screen.getByLabelText(/confirm new password/i);
    await person.type(next, "a-long-enough-password");
    await person.type(confirm, "a-long-enough-password");

    // Matching, long enough, but the current password is still blank.
    expect(screen.getByRole("button", { name: /^save$/i })).toBeDisabled();

    await person.type(screen.getByLabelText(/current password/i), "whatever-it-is");
    const save = screen.getByRole("button", { name: /^save$/i });
    expect(save).toBeEnabled();
    await person.click(save);

    expect(apiMock.setAdminPassword).toHaveBeenCalledWith(
      "a-long-enough-password",
      "whatever-it-is",
    );
  });
});
