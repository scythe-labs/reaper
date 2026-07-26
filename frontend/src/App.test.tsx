// SPDX-License-Identifier: AGPL-3.0-or-later
// The always-visible bits of the shell that must not go quiet when a request fails.
//
// ScanFreshness is the only staleness signal on the review screen, so a failed fetch has to
// read as a failure rather than as "no scan has run yet" (which is a positive claim, and the
// wrong one). UserMenu's sign-out failure notice has to survive the focus move that
// disabling its own button causes. SectionNav keeps its section names when the phone bar drops
// to icons, and its armed mark must not read as "off" when the safety state is unreadable.
import { QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { testQueryClient } from "./test/queryClient";
import { ScanFreshness, ScanLine, SectionNav, UserMenu } from "./App";
import { ApiError, type AuthUser, type Safety, type Snapshot } from "./api";

const { apiMock } = vi.hoisted(() => ({ apiMock: { logout: vi.fn(), safety: vi.fn() } }));

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

describe("ScanLine", () => {
  it("is hidden and announces nothing while idle", () => {
    render(<ScanLine running={false} percent={0} />);
    // aria-hidden takes it out of the accessibility tree, so it is only found with hidden.
    const bar = screen.getByRole("progressbar", { hidden: true });
    expect(bar).toHaveAttribute("aria-hidden", "true");
    expect(bar).not.toHaveAttribute("aria-valuenow");
  });

  it("shows and reports the percent while a scan runs", () => {
    render(<ScanLine running percent={42} />);
    const bar = screen.getByRole("progressbar");
    expect(bar).toHaveAttribute("aria-hidden", "false");
    expect(bar).toHaveAttribute("aria-valuenow", "42");
    expect(bar).toHaveAccessibleName(/scanning your library/i);
  });

  it("clamps an out-of-range percent to the fill width", () => {
    const { container, rerender } = render(<ScanLine running percent={150} />);
    expect(container.querySelector(".scanline-fill")).toHaveStyle({ width: "100%" });
    rerender(<ScanLine running percent={-10} />);
    expect(container.querySelector(".scanline-fill")).toHaveStyle({ width: "0%" });
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
  const queryClient = testQueryClient();
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

const SAFETY: Safety = { destructive_enabled: false, has_password: true, note: null };

function renderNav(view: "review" | "reap" = "review") {
  return render(
    <QueryClientProvider client={testQueryClient()}>
      <SectionNav view={view} onChange={() => {}} />
    </QueryClientProvider>,
  );
}

/** The section nav is the one control that exists at every width, and under 900px it is drawn
 *  as icons alone. Everything below is about what survives losing the words: the names a screen
 *  reader still has, and the safety mark that must never read as "off" when it is really
 *  "couldn't tell". */
describe("SectionNav", () => {
  beforeEach(() => {
    apiMock.safety.mockReset();
  });

  it("names every section, which is all the phone's icon-only bar has to go on", async () => {
    apiMock.safety.mockResolvedValue(SAFETY);
    renderNav();
    for (const label of ["Review", "Policy", "Reap", "Scales", "Settings"]) {
      expect(screen.getByRole("button", { name: label })).toBeInTheDocument();
    }
    await act(async () => {});
  });

  it("states which section you are on rather than only coloring it", async () => {
    apiMock.safety.mockResolvedValue(SAFETY);
    renderNav("reap");
    expect(screen.getByRole("button", { name: "Reap" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("button", { name: "Review" })).not.toHaveAttribute("aria-current");
    await act(async () => {});
  });

  it("marks Reap, and only Reap, while deletion is armed", async () => {
    apiMock.safety.mockResolvedValue({ ...SAFETY, destructive_enabled: true });
    const { container } = renderNav();
    await waitFor(() => expect(container.querySelector(".view-armed-armed")).toBeInTheDocument());
    expect(
      screen.getByRole("button", { name: "Reap" }).querySelector(".view-armed"),
    ).toBeInTheDocument();
    expect(container.querySelectorAll(".view-armed")).toHaveLength(1);
  });

  it("draws no mark while deletion is off", async () => {
    apiMock.safety.mockResolvedValue(SAFETY);
    const { container } = renderNav();
    await act(async () => {});
    expect(container.querySelector(".view-armed")).not.toBeInTheDocument();
  });

  // No mark means "not armed", so falling through to no mark on a failed read would be the
  // fail-open direction on a safety surface: an unreadable state has to look different from a
  // known-safe one (rule 17/36), in the same amber the banner uses to say it could not look.
  it("shows the unknown mark when the safety state cannot be read", async () => {
    apiMock.safety.mockRejectedValue(new ApiError(500, "boom"));
    const { container } = renderNav();
    await waitFor(() => expect(container.querySelector(".view-armed-unknown")).toBeInTheDocument());
    expect(container.querySelector(".view-armed-armed")).not.toBeInTheDocument();
  });

  it("draws nothing on the very first fetch, which knows nothing yet", () => {
    apiMock.safety.mockReturnValue(new Promise(() => {}));
    const { container } = renderNav();
    expect(container.querySelector(".view-armed")).not.toBeInTheDocument();
  });
});
