// SPDX-License-Identifier: AGPL-3.0-or-later
// The two always-visible bits of the shell that must not go quiet when a request fails.
//
// ScanFreshness is the only staleness signal on the review screen, so a failed fetch has to
// read as a failure rather than as "no scan has run yet" (which is a positive claim, and the
// wrong one). UserMenu's sign-out failure notice has to survive the focus move that
// disabling its own button causes.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ScanFreshness, UserMenu } from "./App";
import { ApiError, type AuthUser, type Snapshot } from "./api";

const { apiMock } = vi.hoisted(() => ({ apiMock: { logout: vi.fn() } }));

// Partial mock: ApiError and the types stay real, because ScanFreshness branches on a real
// instance of it.
vi.mock("./api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./api")>()),
  api: apiMock,
}));

const snapshot: Snapshot = {
  id: 1,
  created_at: "2026-01-01T00:00:00+00:00",
  policy_hash: "p",
  horizon_at: "2025-01-01T00:00:00+00:00",
  item_count: 12,
  degraded: false,
  degraded_reason: null,
  condemned: 3,
  protected: 4,
  abstained: 5,
  unknown_size_items: 0,
  reclaimable_bytes: 0,
};

describe("ScanFreshness", () => {
  it("says it is still checking while the query is pending", () => {
    render(
      <ScanFreshness snapshot={undefined} isPending error={undefined} onGoToJobs={() => {}} />,
    );
    expect(screen.getByText(/checking the last scan/i)).toBeInTheDocument();
  });

  it("reads a 404 as the genuine no-scan-yet state", () => {
    render(
      <ScanFreshness
        snapshot={undefined}
        isPending={false}
        error={new ApiError(404, "not found")}
        onGoToJobs={() => {}}
      />,
    );
    expect(screen.getByText(/no scan has run yet/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /settings/i })).toBeInTheDocument();
  });

  it("says the last scan could not be read on any other failure", () => {
    const { container } = render(
      <ScanFreshness
        snapshot={undefined}
        isPending={false}
        error={new ApiError(500, "boom")}
        onGoToJobs={() => {}}
      />,
    );
    expect(screen.queryByText(/no scan has run yet/i)).not.toBeInTheDocument();
    expect(screen.getByText(/couldn't read the last scan/i)).toBeInTheDocument();
    expect(container.querySelector(".notice.notice-error")).not.toBeNull();
  });

  it("shows the incomplete-scan warning when a scan did come back degraded", () => {
    render(
      <ScanFreshness
        snapshot={{ ...snapshot, degraded: true }}
        isPending={false}
        error={undefined}
        onGoToJobs={() => {}}
      />,
    );
    expect(screen.getByText(/came back incomplete/i)).toBeInTheDocument();
  });
});

const user: AuthUser = {
  id: 1,
  username: "owner",
  provider: "local",
  email: null,
  thumb_url: null,
};

function renderMenu() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <UserMenu user={user} />
    </QueryClientProvider>,
  );
}

describe("UserMenu", () => {
  beforeEach(() => {
    apiMock.logout.mockReset();
  });

  it("keeps the panel open so a failed sign-out can be read", async () => {
    apiMock.logout.mockRejectedValue(new ApiError(500, "boom"));
    const person = userEvent.setup();
    renderMenu();

    await person.click(screen.getByRole("button", { name: /owner/i }));
    await person.click(screen.getByRole("button", { name: /sign out/i }));
    expect(await screen.findByText(/couldn't sign you out/i)).toBeInTheDocument();

    // Disabling the focused button moves focus off it, and a click elsewhere is the other
    // way the panel used to close. Neither may take the failure off screen.
    await person.click(document.body);
    expect(screen.getByText(/couldn't sign you out/i)).toBeInTheDocument();
  });

  it("does not show a stale failure the next time the menu opens", async () => {
    apiMock.logout.mockRejectedValue(new ApiError(500, "boom"));
    const person = userEvent.setup();
    renderMenu();

    await person.click(screen.getByRole("button", { name: /owner/i }));
    await person.click(screen.getByRole("button", { name: /sign out/i }));
    expect(await screen.findByText(/couldn't sign you out/i)).toBeInTheDocument();

    await person.keyboard("{Escape}");
    await person.click(screen.getByRole("button", { name: /owner/i }));
    expect(screen.queryByText(/couldn't sign you out/i)).not.toBeInTheDocument();
  });
});
