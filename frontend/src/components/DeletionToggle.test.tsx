// SPDX-License-Identifier: AGPL-3.0-or-later
// The deletion switch is safety UI, so it never renders nothing and never reads as safe on a
// state it could not confirm. It also never strands the operator: the direction that can only
// make Reaper safer stays one click away, whatever the server said.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { DeletionToggle } from "./DeletionToggle";

const { apiMock } = vi.hoisted(() => ({
  apiMock: { safety: vi.fn(), setDeletion: vi.fn() },
}));

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  api: apiMock,
}));

function renderToggle() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <DeletionToggle />
    </QueryClientProvider>,
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
    expect(await screen.findByText(/Reaper is read-only/i)).toBeInTheDocument();
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
