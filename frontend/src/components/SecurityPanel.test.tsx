// SPDX-License-Identifier: AGPL-3.0-or-later
// The admin password arms deletion and is the anti-lockout fallback, so the form must not let
// a typo through: it confirms the new password, and it says out loud why Save is off (too
// short, with a live count; or the two entries disagree) instead of a silently gray button.
import { QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { expectNoA11yViolations } from "../test/a11y";
import { SIGNED_IN_USER } from "../test/apiFixtures";
import { fill } from "../test/forms";
import { testQueryClient } from "../test/queryClient";
import { SecurityPanel } from "./Settings";

const { apiMock } = vi.hoisted(() => ({
  apiMock: {
    safety: vi.fn(),
    setAdminPassword: vi.fn(async () => ({ ok: true })),
    // The form reads ["me"] to learn whether a recovery code opened this session, which is the
    // one case the server takes a new password without the current one. Omitting it would hand
    // the query `undefined`, and the form would render its strict branch off a FAILED read
    // rather than off a real answer -- the shape rule 135 exists to fail the run on.
    me: vi.fn(),
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
    apiMock.me.mockReset();
    apiMock.me.mockResolvedValue(SIGNED_IN_USER);
  });

  // This password arms deletion and is the way back in after a lockout, and the form says why
  // Save is off through the boxes' own descriptions. A box that loses its name or its
  // description leaves the operator with a gray button and no reason for it.
  it("has no accessibility violations", async () => {
    apiMock.safety.mockResolvedValue({ has_password: false });
    renderPanel();
    await screen.findByLabelText(/^new password$/i);
    await expectNoA11yViolations();
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

  // #394. One box carries two kinds of message and they must not share a voice: the live count
  // above changes on EVERY keystroke while the node stays mounted, and a `role="alert"` announces
  // content changes, so typing a valid password re-read the whole string about eleven times on
  // the form that sets the key arming deletion. A failed submit through the same box is the
  // opposite and has to keep interrupting. Both directions are driven, because a flag that
  // silenced one would silence the other just as quietly.
  it("does not interrupt while the operator is still typing", async () => {
    apiMock.safety.mockResolvedValue({ has_password: false });
    const person = renderPanel();

    const next = await screen.findByLabelText(/^new password$/i);
    await person.type(next, "hunter7");

    const complaint = screen.getByText(/use at least 12 characters/i).closest(".notice");
    expect(complaint).not.toHaveAttribute("role", "alert");
    // Nothing is lost by not interrupting: the box the operator is standing in points at this
    // very node, so a reader gets the complaint as that box's description.
    expect(next).toHaveAttribute("aria-describedby", complaint?.id);
  });

  it("still interrupts when the save itself is refused", async () => {
    apiMock.safety.mockResolvedValue({ has_password: false });
    apiMock.setAdminPassword.mockRejectedValue(new Error("the server said no"));
    const person = renderPanel();

    const next = await screen.findByLabelText(/^new password$/i);
    const confirm = screen.getByLabelText(/confirm new password/i);
    await fill(person, next, "a-long-enough-password");
    await fill(person, confirm, "a-long-enough-password");

    const save = screen.getByRole("button", { name: /^save$/i });
    // Rule 137: user-event reports a press on a disabled control as a success.
    await waitFor(() => expect(save).toBeEnabled());
    await person.click(save);

    // Every live branch is clear by now -- `valid` requires it before Save can be pressed at all
    // -- so this notice mounts fresh, which is the insertion `role="alert"` is announced on.
    const failure = await screen.findByText(/the password wasn't set/i);
    expect(failure.closest(".notice")).toHaveAttribute("role", "alert");
  });

  it("blocks Save until the confirmation matches, then sends only the new password", async () => {
    apiMock.safety.mockResolvedValue({ has_password: false });
    const person = renderPanel();

    const next = await screen.findByLabelText(/^new password$/i);
    const confirm = screen.getByLabelText(/confirm new password/i);
    await fill(person, next, "a-long-enough-password");
    await fill(person, confirm, "a-long-enough-passwerd"); // typo

    expect(screen.getByText(/the passwords don't match/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^save$/i })).toBeDisabled();

    // Fix the typo -> Save enables and only the new password is sent.
    await fill(person, confirm, "a-long-enough-password");
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
    await fill(person, next, "a-long-enough-password");
    await fill(person, confirm, "a-long-enough-password");

    // Matching, long enough, but the current password is still blank.
    expect(screen.getByRole("button", { name: /^save$/i })).toBeDisabled();
    // ...and the form says so. This was the one refusal of the three with no arm in `errorNode`:
    // Save went gray and nothing on the page named the box it was waiting on (#188). Bound to
    // that box, because a `disabled` button is out of the Tab order.
    const currentBox = screen.getByLabelText(/current password/i);
    // Regex, because `Notice tone="error"` prefixes its text with a visually-hidden "Problem:"
    // that belongs to the notice rather than to this sentence.
    expect(currentBox).toHaveAccessibleDescription(/enter the current password to save\./i);

    await fill(person, currentBox, "whatever-it-is");
    expect(currentBox).toHaveAccessibleDescription("");
    const save = screen.getByRole("button", { name: /^save$/i });
    expect(save).toBeEnabled();
    await person.click(save);

    expect(apiMock.setAdminPassword).toHaveBeenCalledWith(
      "a-long-enough-password",
      "whatever-it-is",
    );
  });

  it("says nothing about the current password on a form nobody has touched", async () => {
    // The complaint is gated on a new password having been typed, like the two above are gated on
    // their own box. An empty current password is the state this form OPENS in, so an ungated
    // sentence would greet the operator with a failure where there is only a next step.
    apiMock.safety.mockResolvedValue({ has_password: true });
    renderPanel();

    const currentBox = await screen.findByLabelText(/current password/i);
    expect(currentBox).toHaveAccessibleDescription("");
    expect(screen.queryByText(/enter the current password/i)).not.toBeInTheDocument();
    // Off, but for a reason further down the form the operator has not reached yet.
    expect(screen.getByRole("button", { name: /^save$/i })).toBeDisabled();
  });

  it("keeps the complaint the operator can act on when three could fire at once", async () => {
    // One region, now four possible messages, so the order it picks them in is the behavior.
    // A short new password and a blank current password hold together; the length complaint is
    // the one about the box being typed in, so it wins and the current box stays quiet -- the
    // same discipline `errorOwner` was written for (#174), driven through the arm added by #188.
    apiMock.safety.mockResolvedValue({ has_password: true });
    const person = renderPanel();

    const next = await screen.findByLabelText(/^new password$/i);
    const currentBox = screen.getByLabelText(/current password/i);
    await person.type(next, "short");

    expect(next).toHaveAccessibleDescription(/use at least 12 characters/i);
    expect(currentBox).toHaveAccessibleDescription("");

    // Long enough and confirmed: the only thing left is the current password, so it speaks now.
    await person.type(next, "-but-now-long-enough");
    await fill(person, screen.getByLabelText(/confirm new password/i), "short-but-now-long-enough");

    expect(next).toHaveAccessibleDescription("");
    // Regex, because `Notice tone="error"` prefixes its text with a visually-hidden "Problem:"
    // that belongs to the notice rather than to this sentence.
    expect(currentBox).toHaveAccessibleDescription(/enter the current password to save\./i);
  });
});

describe("changing the password after signing in with a recovery code", () => {
  // A forgotten password is what recovery mode is FOR, so this form asking for it left the
  // operator signed in and still locked out of the credential that arms deletion -- and on a
  // desktop build there was no CLI to fall back to (#433). The server takes a new password from
  // a recovery session without the old one; these pin the form agreeing with it.
  beforeEach(() => {
    apiMock.safety.mockReset();
    apiMock.setAdminPassword.mockReset();
    apiMock.setAdminPassword.mockResolvedValue({ ok: true });
    apiMock.me.mockReset();
  });

  it("parks the current-password box and saves without it", async () => {
    apiMock.safety.mockResolvedValue({ has_password: true });
    apiMock.me.mockResolvedValue({ ...SIGNED_IN_USER, via_recovery: true });
    const person = renderPanel();

    const currentBox = await screen.findByLabelText(/current password/i);
    // Still on screen, so a form the operator has used before is still recognizable, but
    // plainly inert and carrying the one line that says why.
    await waitFor(() => expect(currentBox).toBeDisabled());
    expect(screen.getByText(/not needed\. a recovery code signed you in\./i)).toBeInTheDocument();

    await fill(person, screen.getByLabelText(/^new password$/i), "a-long-enough-password");
    await fill(person, screen.getByLabelText(/confirm new password/i), "a-long-enough-password");

    const save = screen.getByRole("button", { name: /^save$/i });
    // Enabled with the current box empty, which is the whole point: on an ordinary session the
    // same three boxes in this state leave Save off (see "needs the current password" above).
    await waitFor(() => expect(save).toBeEnabled());
    await person.click(save);

    // `undefined`, never the empty string: the server reads a present-but-empty current password
    // as an attempt to prove the old one and refuses it with a 403.
    expect(apiMock.setAdminPassword).toHaveBeenCalledWith("a-long-enough-password", undefined);
  });

  it("asks for the current password again once the session's mark is spent", async () => {
    // The server clears the mark in the same transaction that writes the new hash, so the form
    // re-reads ["me"] on success. Without that the box would stay parked over a session that no
    // longer excuses it, and the NEXT change would fail at the API with an empty box on screen.
    apiMock.safety.mockResolvedValue({ has_password: true });
    apiMock.me.mockResolvedValueOnce({ ...SIGNED_IN_USER, via_recovery: true });
    apiMock.me.mockResolvedValue(SIGNED_IN_USER);
    const person = renderPanel();

    const currentBox = await screen.findByLabelText(/current password/i);
    await waitFor(() => expect(currentBox).toBeDisabled());

    await fill(person, screen.getByLabelText(/^new password$/i), "a-long-enough-password");
    await fill(person, screen.getByLabelText(/confirm new password/i), "a-long-enough-password");
    await person.click(screen.getByRole("button", { name: /^save$/i }));

    await waitFor(() => expect(screen.getByLabelText(/current password/i)).toBeEnabled());
    expect(screen.queryByText(/a recovery code signed you in/i)).not.toBeInTheDocument();
  });

  it("demands the current password when the session cannot be read at all", async () => {
    // Rule 17/36: the unknown state is answered explicitly, and the answer is the strict one.
    // A read that fails must never be the thing that unlocks a credential change -- so the
    // fallback here is the ordinary form, identical to a session that is genuinely not a
    // recovery one. This is the branch a `?? false` would be doing silently; it is asserted so
    // that flipping the default to the lenient side cannot pass.
    apiMock.safety.mockResolvedValue({ has_password: true });
    apiMock.me.mockRejectedValue(new Error("no session"));
    const person = renderPanel();

    const currentBox = await screen.findByLabelText(/current password/i);
    await waitFor(() => expect(currentBox).toBeEnabled());
    expect(screen.queryByText(/a recovery code signed you in/i)).not.toBeInTheDocument();

    await fill(person, screen.getByLabelText(/^new password$/i), "a-long-enough-password");
    await fill(person, screen.getByLabelText(/confirm new password/i), "a-long-enough-password");

    expect(screen.getByRole("button", { name: /^save$/i })).toBeDisabled();
  });
});

describe("what leaving this panel would lose", () => {
  // The panel reports its typed password up to the settings shell, so switching section can stop
  // and ask instead of unmounting three boxes silently.
  beforeEach(() => {
    apiMock.safety.mockReset();
    apiMock.setAdminPassword.mockReset();
    apiMock.setAdminPassword.mockResolvedValue({ ok: true });
    apiMock.me.mockReset();
    apiMock.me.mockResolvedValue(SIGNED_IN_USER);
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
    await fill(person, current, "whatever-it-is");
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
    await fill(person, next, "a-long-enough-password");
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
    // And it does NOT say to reload (#153). That line sat directly above these three boxes and
    // named the one action that empties them: there is no `beforeunload` handler anywhere in
    // `frontend/src`, so a reload took the typed password with no ask, from an operator doing what
    // the page told them. The sentence lives in StaleReadNotice.tsx and is shared by seven panels,
    // so this assertion moves with it (rule 144).
    expect(stale).not.toHaveTextContent(/reload/i);
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
