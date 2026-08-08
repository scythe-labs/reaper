// SPDX-License-Identifier: AGPL-3.0-or-later
// The deletion switch is safety UI, so it never renders nothing and never reads as safe on a
// state it could not confirm. It also never strands the operator: the direction that can only
// make Reaper safer stays one click away, whatever the server said.
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { expectNoA11yViolations } from "../test/a11y";
import { renderWithProviders } from "../test/renderWithProviders";
import { Announcer } from "../announce";
import { DeletionToggle } from "./DeletionToggle";

const { apiMock } = await vi.hoisted(async () => ({
  apiMock: (await import("../test/apiMock")).makeApiMock(),
}));

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  api: apiMock,
}));

function renderToggle() {
  renderWithProviders(
    <>
      {/* App mounts this once above every route; the switch speaks into it (#170). */}
      <Announcer />
      <DeletionToggle />
    </>,
  );
  return userEvent.setup();
}

beforeEach(() => {
  vi.clearAllMocks();
  apiMock.setDeletion.mockResolvedValue({ ok: true });
});

describe("when the safety state can't be read", () => {
  // Rejected lazily, per call: a rejected promise built up front is unhandled until the query
  // gets around to it, which the runner reports as a failure of its own.
  beforeEach(() => {
    apiMock.safety.mockImplementation(() => Promise.reject(new Error("unreachable")));
  });

  // In this branch the amber notice and the one Turn off button are everything the operator has:
  // Reaper could not say whether it is armed to delete. A reader that skips either leaves them
  // unable to put the app back into read-only.
  it("has no accessibility violations", async () => {
    renderToggle();
    await screen.findByText(/couldn't confirm whether deletion is on/i);
    await expectNoA11yViolations();
  });

  it("still says so in amber, never in a way that reads as safe", async () => {
    renderToggle();
    expect(await screen.findByText(/couldn't confirm whether deletion is on/i)).toBeInTheDocument();
  });

  it("still offers Turn off, the one direction that can only make Reaper safer", async () => {
    // PR-4: the error branch used to render the amber notice and nothing else, so an operator
    // who wanted read-only RIGHT NOW was handed advice to assume the worst and no button --
    // and had to reload until a GET happened to succeed. Turning deletion off is exactly the
    // direction the backend never gates: no password, no prior state.
    const person = renderToggle();
    const off = await screen.findByRole("button", { name: "Turn off" });

    await person.click(off);

    await waitFor(() => expect(apiMock.setDeletion).toHaveBeenCalledWith(false, undefined));
  });

  it("confirms the result, since the banner above still cannot", async () => {
    const person = renderToggle();
    await person.click(await screen.findByRole("button", { name: "Turn off" }));
    // On screen, and said out loud (#170). Both, because this branch is reached exactly when
    // the state could not be read, so the confirmation IS the operator's only answer -- and an
    // operator driving by ear had none of it.
    expect(await screen.findByText(/Reaper is read-only/i, { selector: "strong" })).toBeVisible();
    expect(
      await screen.findByText(/Reaper is read-only/i, { selector: ".sr-only" }),
    ).toBeInTheDocument();
  });

  it("renders a failed Turn off instead of swallowing it", async () => {
    apiMock.setDeletion.mockImplementation(() =>
      Promise.reject(new Error("The server refused that.")),
    );
    const person = renderToggle();
    await person.click(await screen.findByRole("button", { name: "Turn off" }));
    expect(await screen.findByText("The server refused that.")).toBeInTheDocument();
  });

  it("does NOT offer Turn on, which would be arming against a state we never read", async () => {
    renderToggle();
    await screen.findByRole("button", { name: "Turn off" });
    expect(screen.queryByRole("button", { name: /Turn on/i })).not.toBeInTheDocument();
  });
});

describe("the arming password", () => {
  beforeEach(() => {
    apiMock.safety.mockResolvedValue({
      destructive_enabled: false,
      has_password: true,
      note: null,
    });
  });

  it("is gone once you cancel, not waiting in the field when you come back", async () => {
    // S-5: Cancel closed the form and left the password in component state, so it was still
    // there for as long as Settings stayed open -- and refilled the box on the way back in.
    const person = renderToggle();
    await person.click(await screen.findByRole("button", { name: /Turn on/i }));
    await person.type(screen.getByLabelText(/password/i), "a-password");
    await person.click(screen.getByRole("button", { name: /Cancel/i }));

    await person.click(await screen.findByRole("button", { name: /Turn on/i }));
    expect(screen.getByLabelText(/password/i)).toHaveValue("");
  });

  // #170: this switch decides whether Reaper may remove anything at all, and it used to signal
  // the outcome the way the rest of the app once did -- the form unmounted, a `<strong>` in an
  // unfocused subtree rewrote itself, and focus fell to `<body>`. An operator driving by ear
  // could not tell whether they had just armed the app to delete their library.
  it("says out loud that deletion is on", async () => {
    const person = renderToggle();
    await person.click(await screen.findByRole("button", { name: /Turn on/i }));
    await person.type(screen.getByLabelText(/password/i), "a-password");
    // Arming flips the state the switch reads, so the row it comes back to says "Turn off".
    apiMock.safety.mockResolvedValue({
      destructive_enabled: true,
      has_password: true,
      note: null,
    });
    await person.click(screen.getByRole("button", { name: /Confirm/i }));

    expect(
      await screen.findByText("Deletion is on.", { selector: ".sr-only" }),
    ).toBeInTheDocument();
  });

  it("puts the operator back on the row's button after arming, not on the body", async () => {
    const person = renderToggle();
    await person.click(await screen.findByRole("button", { name: /Turn on/i }));
    await person.type(screen.getByLabelText(/password/i), "a-password");
    apiMock.safety.mockResolvedValue({
      destructive_enabled: true,
      has_password: true,
      note: null,
    });
    await person.click(screen.getByRole("button", { name: /Confirm/i }));

    // The row's action slot, whichever direction it now offers. Focus on `<body>` here means
    // the next Tab restarts at the top of the whole settings page.
    await waitFor(() => expect(screen.getByRole("button", { name: /Turn off/i })).toHaveFocus());
  });

  it("puts the operator back on Turn on… after cancelling", async () => {
    const person = renderToggle();
    const open = await screen.findByRole("button", { name: /Turn on/i });
    await person.click(open);
    await person.click(screen.getByRole("button", { name: /Cancel/i }));

    await waitFor(() => expect(screen.getByRole("button", { name: /Turn on/i })).toHaveFocus());
  });

  it("never claims read-only over a host it just armed", async () => {
    // The arm lands, and the read that would confirm it fails -- a restarted server, a dropped
    // session, or any later poll. `toggle.isSuccess` records only THAT a toggle succeeded, not
    // which way it went, so the unknown branch painted its green "Reaper is read-only" over a
    // host that is armed, while the live region a few nodes away said "Deletion is on."
    const person = renderToggle();
    await person.click(await screen.findByRole("button", { name: /Turn on/i }));
    await person.type(screen.getByLabelText(/password/i), "a-password");
    apiMock.safety.mockImplementation(() => Promise.reject(new Error("unreachable")));
    await person.click(screen.getByRole("button", { name: /Confirm/i }));

    // Unknown reads as unknown, in amber. The green claim is the one thing this state may
    // never make, because it is the reassuring direction to be wrong in.
    expect(await screen.findByText(/couldn't confirm whether deletion is on/i)).toBeInTheDocument();
    expect(
      screen.queryByText(/Reaper is read-only/i, { selector: "strong" }),
    ).not.toBeInTheDocument();
  });
});
