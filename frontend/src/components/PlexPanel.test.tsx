// SPDX-License-Identifier: AGPL-3.0-or-later
// The Plex settings panel. These pin the three things an operator can get stuck on:
// reopening the manual-address editor for an address they typed earlier, getting out of
// a sign-in whose plex.tv tab never opened, and seeing a failed sign-in as a failure.
import { QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { PlexResourceConnection, PlexStatus } from "../api";
import { testQueryClient } from "../test/queryClient";
import { PlexPanel } from "./PlexPanel";

const { apiMock } = vi.hoisted(() => ({
  apiMock: {
    plexStatus: vi.fn(),
    plexResources: vi.fn(),
    plexLibraries: vi.fn(),
    syncPlexLibraries: vi.fn(),
    setPlexLibraries: vi.fn(),
    leavingSoonSettings: vi.fn(),
    setLeavingSoonSettings: vi.fn(),
    syncLeavingSoon: vi.fn(),
    setPlexWebUrl: vi.fn(),
    plexSetConnection: vi.fn(),
    plexSwitchServer: vi.fn(),
    plexUnlink: vi.fn(),
    plexLinkStart: vi.fn(),
    plexLinkPoll: vi.fn(),
  },
}));

vi.mock("../api", () => ({ api: apiMock }));

const LOCAL = "https://10-0-0-2.abcdef.plex.direct:32400";
const TYPED = "https://plex.example.net:32400";

function status(overrides: Partial<PlexStatus> = {}): PlexStatus {
  return {
    linked: true,
    name: "Example server",
    connection_uri: TYPED,
    last_ok_at: null,
    verify_tls: true,
    web_url: "https://app.plex.tv",
    ...overrides,
  };
}

function discovered(uri: string): PlexResourceConnection {
  return { uri, local: true, relay: false, protocol: "https" };
}

/** The connection picker, found through the option every state renders. */
async function connectionSelect(): Promise<HTMLSelectElement> {
  const manual = await screen.findByRole("option", { name: "Manual address…" });
  return manual.closest("select") as HTMLSelectElement;
}

function renderPanel(connections: PlexResourceConnection[] = [discovered(LOCAL)]) {
  apiMock.plexResources.mockResolvedValue({
    source: "plex.tv",
    servers: [
      {
        name: "Example server",
        machine_identifier: "machine-1",
        current: true,
        connections,
      },
    ],
  });
  const queryClient = testQueryClient();
  return render(
    <QueryClientProvider client={queryClient}>
      <PlexPanel />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  apiMock.plexStatus.mockResolvedValue(status());
  apiMock.plexLibraries.mockResolvedValue([
    { key: 1, title: "Movies", kind: "movie", enabled: true },
  ]);
  apiMock.syncPlexLibraries.mockResolvedValue([]);
  apiMock.setPlexLibraries.mockResolvedValue([]);
  apiMock.leavingSoonSettings.mockResolvedValue({
    enabled: false,
    allow_unarmed: false,
    last: null,
  });
  apiMock.setPlexWebUrl.mockResolvedValue(status());
  apiMock.plexSetConnection.mockResolvedValue(status());
  apiMock.plexLinkStart.mockResolvedValue({ pin_id: 1, auth_url: "https://plex.tv/link/pin" });
  apiMock.plexLinkPoll.mockResolvedValue({ status: "pending", server: null, servers: null });
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("the connection picker", () => {
  it("reopens the editor for an address that was typed by hand", async () => {
    const user = userEvent.setup();
    renderPanel();

    const connection = await connectionSelect();
    // The typed address is its own option, and it is the one selected.
    expect(connection.value).toBe(TYPED);
    expect(screen.getByRole("option", { name: `Manual · ${TYPED}` })).toBeInTheDocument();

    // "Manual address…" is a separate option, so picking it always fires a change.
    await user.selectOptions(connection, "__manual__");
    expect(await screen.findByText("Manual address")).toBeInTheDocument();
    expect(screen.getByPlaceholderText("plex.example.net")).toHaveValue("plex.example.net");
    expect(apiMock.plexSetConnection).not.toHaveBeenCalled();
  });

  it("offers the editor even when no addresses were discovered", async () => {
    const user = userEvent.setup();
    renderPanel([]);

    await user.selectOptions(await connectionSelect(), "__manual__");
    expect(await screen.findByText("Manual address")).toBeInTheDocument();
  });
});

describe("when plex.tv's list comes back without the linked server", () => {
  it("says so and offers no picker, instead of presenting some other server as ours", async () => {
    // B-10: `currentServer` fell back to `servers[0]`, so a partial or filtered plex.tv
    // response silently promoted a DIFFERENT server to "the one Reaper manages" and the
    // Connection row listed that server's addresses. Saving one pointed Reaper's Leaving
    // Soon writes and its Never-Reap read at a library it was never linked to.
    apiMock.plexResources.mockResolvedValue({
      source: "plex.tv",
      owner_username: "reaper-owner",
      servers: [
        {
          name: "Someone else's server",
          machine_identifier: "machine-other",
          current: false,
          connections: [discovered("https://10-0-0-9.abcdef.plex.direct:32400")],
        },
      ],
    });
    const queryClient = testQueryClient();
    render(
      <QueryClientProvider client={queryClient}>
        <PlexPanel />
      </QueryClientProvider>,
    );

    // The row says what happened, and names the server Reaper is actually linked to.
    const notice = await screen.findByText(/came back without the server Reaper uses/);
    expect(notice).toHaveClass("notice-warn");
    expect(notice.textContent).toContain("Example server");

    // The other server is not offered at all -- not even as an option a browser would
    // display: a select whose value matches nothing shows its first option, so listing it
    // would still read as "this is your server", merely unsavable. The box names the
    // linked server, and neither picker can act.
    expect(screen.queryByRole("option", { name: "Someone else's server" })).not.toBeInTheDocument();
    const server = screen.getByRole("option", { name: "Example server" }).closest("select");
    expect(server).toBeDisabled();
    expect(await connectionSelect()).toBeDisabled();

    // And the other server's addresses are not on offer either.
    expect(screen.queryByRole("option", { name: /10-0-0-9/ })).not.toBeInTheDocument();
    expect(apiMock.plexSetConnection).not.toHaveBeenCalled();
  });
});

describe("linking with Plex", () => {
  it("offers the approval link and a way out while it waits", async () => {
    const user = userEvent.setup();
    apiMock.plexStatus.mockResolvedValue(
      status({ linked: false, name: null, connection_uri: null }),
    );
    renderPanel();

    await user.click(await screen.findByRole("button", { name: "Link with Plex" }));

    const link = await screen.findByRole("link", { name: "Didn’t open?" });
    expect(link).toHaveAttribute("href", "https://plex.tv/link/pin");

    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(await screen.findByRole("button", { name: "Link with Plex" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Didn’t open?" })).not.toBeInTheDocument();
  });

  it("reports a sign-in that never finished as a failure, not as status", async () => {
    apiMock.plexStatus.mockResolvedValue(
      status({ linked: false, name: null, connection_uri: null }),
    );
    renderPanel();
    const start = await screen.findByRole("button", { name: "Link with Plex" });

    vi.useFakeTimers();
    try {
      fireEvent.click(start);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0); // the PIN request settles
        // Past the five-minute approval deadline, with nobody approving.
        await vi.advanceTimersByTimeAsync(5 * 60 * 1000 + 4000);
      });

      const timedOut = screen.getByText("Plex sign-in timed out. Try again.");
      expect(timedOut).toHaveClass("notice-error");
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("the signed-in account label", () => {
  it("never flashes the server name while the account name is loading", async () => {
    let resolveResources: (value: unknown) => void = () => {};
    apiMock.plexResources.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveResources = resolve;
        }),
    );
    const queryClient = testQueryClient();
    render(
      <QueryClientProvider client={queryClient}>
        <PlexPanel />
      </QueryClientProvider>,
    );

    // Past the fast, local status query, the account row is up, but the live plex.tv
    // account lookup is still in flight.
    await screen.findByRole("button", { name: "Unlink" });
    await waitFor(() => expect(apiMock.plexResources).toHaveBeenCalled());

    // While that lookup is in flight, the row shows a neutral placeholder, never the
    // server name ("Example server") the status query already has.
    expect(screen.getByText("Loading…")).toBeInTheDocument();
    expect(screen.queryByText("Example server")).not.toBeInTheDocument();

    await act(async () => {
      resolveResources({ source: "plex.tv", servers: [], owner_username: "reaper-owner" });
    });

    expect(await screen.findByText("reaper-owner")).toBeInTheDocument();
  });
});

describe("the certificate check", () => {
  it("warns beside the switch that turned it off", async () => {
    const user = userEvent.setup();
    // The saved value follows the server, so the save has to answer with the new one or
    // the refetch would flip the switch straight back on.
    apiMock.setPlexWebUrl.mockImplementation(async (_url: string, verify?: boolean) => {
      const next = status({ verify_tls: verify ?? true });
      apiMock.plexStatus.mockResolvedValue(next);
      return next;
    });
    renderPanel();

    const toggle = await screen.findByRole("switch", { name: "Check the server's certificate" });
    await user.click(toggle);

    const warning = await waitFor(() =>
      screen.getByText(/accept this server's certificate without checking/),
    );
    const row = warning.closest(".set-row");
    expect(row).not.toBeNull();
    // Inside the same row as its switch, not adrift below the whole group.
    expect(within(row as HTMLElement).getByRole("switch")).toBe(toggle);
  });
});
