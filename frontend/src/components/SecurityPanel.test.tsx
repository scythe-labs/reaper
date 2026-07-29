// SPDX-License-Identifier: AGPL-3.0-or-later
// The admin password arms deletion and is the anti-lockout fallback, so the form must not let
// a typo through: it confirms the new password, and it says out loud why Save is off (too
// short, with a live count; or the two entries disagree) instead of a silently gray button.
import { QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
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

function renderPanel(
  /** Stable across renders, which the prop requires: pass one `vi.fn()`, never an inline arrow. */
  onDirtyChange?: (dirty: boolean) => void,
  /** Passed in only when the test needs to drive a refetch itself. */
  queryClient = testQueryClient(),
) {
  render(
    <QueryClientProvider client={queryClient}>
      <SecurityPanel onDirtyChange={onDirtyChange} />
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

  it("never describes a box with the other box's complaint", async () => {
    // Both boxes point at ONE region, and `tooShort` and `mismatch` are independent, so a short
    // password with a non-matching confirm holds both at once while the region shows only the
    // first. Gated on the bare predicates, the confirm box read out "use at least 12
    // characters" -- the box above it -- and the mismatch text was not on the page to reach.
    // Asserted as the accessible DESCRIPTION, which is what a reader computes: an
    // `aria-describedby` naming an id that is not rendered satisfies an attribute check and
    // still says nothing.
    apiMock.safety.mockResolvedValue({ has_password: false });
    const person = renderPanel();

    const next = await screen.findByLabelText(/^new password$/i);
    const confirm = screen.getByLabelText(/confirm new password/i);
    await person.type(next, "hunter7");
    await person.type(confirm, "x");

    expect(confirm).not.toHaveAccessibleDescription(/use at least 12 characters/i);
    expect(next).toHaveAccessibleDescription(/use at least 12 characters/i);

    // And once the length complaint clears, the mismatch is the live one and moves to the box
    // it is actually about.
    await person.type(next, "-long-enough");
    expect(await screen.findByText(/the passwords don't match/i)).toBeInTheDocument();
    expect(confirm).toHaveAccessibleDescription(/don't match/i);
    expect(next).not.toHaveAccessibleDescription(/don't match/i);
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

describe("what leaving this panel would lose", () => {
  // The panel reports its typed password up to the settings shell, so switching section can stop
  // and ask instead of unmounting three boxes silently.
  beforeEach(() => {
    apiMock.safety.mockReset();
    apiMock.setAdminPassword.mockReset();
    apiMock.setAdminPassword.mockResolvedValue({ ok: true });
  });

  it("reports a password too short to save, because leaving still throws it away", async () => {
    // `valid` is the wrong signal here: a password Save refuses is still text the operator typed,
    // and reporting only the saveable form would drop exactly the half-finished ones in silence.
    apiMock.safety.mockResolvedValue({ has_password: false });
    const dirty = vi.fn();
    const person = renderPanel(dirty);

    const next = await screen.findByLabelText(/^new password$/i);
    await waitFor(() => expect(dirty).toHaveBeenLastCalledWith(false));
    await person.type(next, "short");

    expect(screen.getByRole("button", { name: /^save$/i })).toBeDisabled();
    await waitFor(() => expect(dirty).toHaveBeenLastCalledWith(true));
  });

  it("stops reporting once the boxes are empty again", async () => {
    apiMock.safety.mockResolvedValue({ has_password: true });
    const dirty = vi.fn();
    const person = renderPanel(dirty);

    const current = await screen.findByLabelText(/current password/i);
    await person.type(current, "whatever-it-is");
    await waitFor(() => expect(dirty).toHaveBeenLastCalledWith(true));

    await person.clear(current);
    await waitFor(() => expect(dirty).toHaveBeenLastCalledWith(false));
  });

  it("keeps the form when a refetch fails, so the typed password stays reachable", async () => {
    // Rule 146: the report makes two claims at once -- there is something to lose, AND the
    // operator can still get to it. This panel's draft lives in a CHILD, so an early return here
    // does not hide the form, it unmounts it and takes three typed boxes with it. And `useSafety`
    // polls every 15 seconds, so one failed poll reached that state with the operator doing
    // nothing but typing. React Query keeps the last good row through a failed refetch, so the
    // form stays on it; the old `isError || !data` branch traded it for one paragraph.
    apiMock.safety.mockResolvedValue({ has_password: true });
    const dirty = vi.fn();
    const queryClient = testQueryClient();
    const person = renderPanel(dirty, queryClient);

    const next = await screen.findByLabelText(/^new password$/i);
    await person.type(next, "a-long-enough-password");
    await waitFor(() => expect(dirty).toHaveBeenLastCalledWith(true));

    // In the app this arrives on `useSafety`'s own 15-second poll, which no test should sit out;
    // the poll and this are the same refetch, so ask for one directly.
    apiMock.safety.mockRejectedValue(new Error("server unreachable"));
    await act(() => queryClient.invalidateQueries({ queryKey: ["safety"] }));

    // Still the form, still holding the draft, and still saying the read failed (rule 17/36):
    // everything below is presented as current otherwise, and it is known to be stale.
    expect(screen.getByLabelText(/^new password$/i)).toHaveValue("a-long-enough-password");
    expect(screen.queryByText(/Couldn't load these settings/)).toBeNull();
    const stale = await screen.findByText(/Couldn't check these settings just now/);
    expect(stale).toHaveClass("notice-warn");
    expect(dirty).toHaveBeenLastCalledWith(true);
  });

  it("reports nothing when the first read never lands, because there is no form to lose", async () => {
    // The other side of rule 146: with no stored row there is no form, so a guard that fired here
    // would demand a discard for boxes that were never on screen.
    apiMock.safety.mockRejectedValue(new Error("server unreachable"));
    const dirty = vi.fn();
    renderPanel(dirty);

    expect(await screen.findByText(/Couldn't load these settings/)).toBeInTheDocument();
    expect(screen.queryByLabelText(/^new password$/i)).toBeNull();
    expect(dirty).not.toHaveBeenCalledWith(true);
  });
});
