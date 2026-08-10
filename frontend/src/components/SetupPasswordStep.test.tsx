// SPDX-License-Identifier: AGPL-3.0-or-later
// The wizard's password step, and who hears its one error region.
//
// The step is a second drawing of `AdminPasswordForm`: same two boxes, same two complaints, same
// single region carrying whichever one is live. What it had drifted on is the half that is not
// visible in a diff -- the live complaints were `role="alert"`, and one of them renders
// `{pw.length} so far`, so its text changed inside a live region on every keystroke and the whole
// string was re-announced each time, on the form that sets the key arming deletion. Both arms are
// driven here, because the fix is a branch and only one side of it is the interesting one.
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { expectNoA11yViolations } from "../test/a11y";
import { renderWithProviders } from "../test/renderWithProviders";
import { SetupPasswordStep } from "./SetupPasswordStep";

const { apiMock } = await vi.hoisted(async () => ({
  apiMock: (await import("../test/apiMock")).makeApiMock(),
}));

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  api: apiMock,
}));

describe("the wizard's password step", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiMock.setAdminPassword.mockResolvedValue({ ok: true });
  });

  function renderStep() {
    renderWithProviders(<SetupPasswordStep onDone={vi.fn()} />);
    return userEvent.setup();
  }

  const box = () => screen.getByLabelText(/^Password/);
  const confirmBox = () => screen.getByLabelText(/^Confirm password/);

  // The first thing a new install asks for, and the credential that later confirms every
  // deletion. Audited with a complaint up, since that is the state the boxes gain a description
  // in and the one this file is about.
  it("has no accessibility violations", async () => {
    const person = renderStep();
    await person.type(box(), "hunter7");

    await expectNoA11yViolations();
  });

  it("does not interrupt while the operator is still typing", async () => {
    const person = renderStep();

    await person.type(box(), "hunter7");

    const complaint = screen.getByText(/Use at least 12 characters/i).closest(".notice");
    expect(complaint).not.toBeNull();
    expect(screen.getByText(/7 so far/i)).toBeInTheDocument();
    // `standing`: the sentence explains why the button is off, it is reachable from the box
    // through `aria-describedby`, and it rewrites itself on the next keystroke.
    expect(complaint).not.toHaveAttribute("role");
    expect(box()).toHaveAccessibleDescription(/Use at least 12 characters/i);
  });

  it("still announces a submit that failed, which is a reaction", async () => {
    apiMock.setAdminPassword.mockRejectedValue(new Error("Reaper couldn't save it."));
    const person = renderStep();

    await person.type(box(), "a-long-enough-one");
    await person.type(confirmBox(), "a-long-enough-one");
    await person.click(screen.getByRole("button", { name: /Set password and continue/i }));

    const failed = await screen.findByText(/That didn't save/i);
    expect(failed.closest(".notice")).toHaveAttribute("role", "alert");
  });
});
