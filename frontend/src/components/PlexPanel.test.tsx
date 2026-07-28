// SPDX-License-Identifier: AGPL-3.0-or-later
// The Plex settings panel. These pin the things an operator can get stuck on: reopening the
// manual-address editor for an address they typed earlier, getting out of a sign-in whose
// plex.tv tab never opened, seeing a failed sign-in as a failure, and -- for anyone driving
// this panel by ear -- being able to tell one box on it from another.
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
function connectionSelect(): HTMLSelectElement {
  const manual = screen.getByRole("option", { name: "Manual address…" });
  return manual.closest("select") as HTMLSelectElement;
}

/** The same picker, once the plex.tv lookup has answered and it can actually be used.
 *
 *  Its options come from the fast, local status read, so the picker is on screen a turn before
 *  it works: it stays disabled while the lookup runs. user-event reports a disabled select as
 *  SUCCESS -- `selectOptions` returns having dispatched nothing -- so a test that acted in that
 *  window silently did nothing, then failed a second later on an editor that never opened. These
 *  two passed only because the mocked lookup is an already-resolved promise: one event-loop turn
 *  of slack, which a loaded machine hands out freely, is the whole margin. Rule 137. */
async function usableConnectionSelect(): Promise<HTMLSelectElement> {
  await waitFor(() => expect(connectionSelect()).toBeEnabled());
  return connectionSelect();
}

function renderPanel(
  connections: PlexResourceConnection[] = [discovered(LOCAL)],
  /** Stable across renders, which the prop requires: pass one `vi.fn()`, never an inline arrow. */
  onDirtyChange?: (dirty: boolean) => void,
) {
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
      <PlexPanel onDirtyChange={onDirtyChange} />
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

    const connection = await usableConnectionSelect();
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

    await user.selectOptions(await usableConnectionSelect(), "__manual__");
    expect(await screen.findByText("Manual address")).toBeInTheDocument();
  });
});

describe("every control on this panel", () => {
  // Rule 18: a `.set-row`'s label is a `<span className="set-label">`, which names nothing. The
  // rows whose control is a Switch escape that because Switch takes `ariaLabel` and every one of
  // them passes it; the rows carrying a box had no accessible name at all, so a screen reader
  // announced the server picker, the connection picker and the web address as three bare fields
  // with no way to tell them apart -- and the connection picker is what points Reaper at the
  // server it manages.
  //
  // Rule 145: this walks a population, so it counts. "Every control I collected has a name" reads
  // green when the walk collects nothing, and the pickers were reached here through
  // `getByRole("option").closest("select")` for exactly that reason -- a shape that never asks
  // for a name. The table below is every input and select this fixture renders, reconciled
  // against the source by hand; the count is what makes an eleventh control name itself rather
  // than opt out -- IN THE BRANCHES THIS FIXTURE MOUNTS. That qualifier is the honest limit of
  // the guard and is load-bearing: a control added to a branch the walk never renders is missing
  // from the table and from the count alike, and the two absences hide each other perfectly
  // (rule 145's own blind spot). Measured rather than assumed -- a bare `<input>` dropped into
  // the `resources.isError` arm leaves this file green. The branches NOT walked here are
  // `resources.isError`, `linkedServerMissing`, the unlinked `ServerPickList`, and the pending
  // and `!data` arms; none renders a control today, which is what makes the count correct now,
  // and none is watched by this test if that changes.
  it("answers to the label the operator can see", async () => {
    const user = userEvent.setup();
    const { container } = renderPanel();

    // The manual editor is closed at rest and its two boxes belong to the population, so open it.
    await user.selectOptions(await usableConnectionSelect(), "__manual__");
    // The last section to arrive, so waiting on it settles the libraries above it too.
    await screen.findByRole("switch", { name: "Update while read-only" });

    const controls: [name: string, tag: string][] = [
      ["Server", "select"],
      ["Connection", "select"],
      ["Manual address host or IP", "input"],
      ["Manual address port", "input"],
      ["Use SSL", "input"],
      ["Check the server's certificate", "input"],
      ["Plex web address", "input"],
      ["Let Reaper touch Movies", "input"],
      ['Show "Leaving Soon" in Plex', "input"],
      ["Update while read-only", "input"],
    ];
    for (const [name, tag] of controls) {
      expect(screen.getByLabelText(name).tagName.toLowerCase()).toBe(tag);
    }

    expect(container.querySelectorAll("input, select")).toHaveLength(controls.length);
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
    expect(connectionSelect()).toBeDisabled();

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

describe("what leaving this panel would lose", () => {
  // The panel reports its unsaved drafts up to the settings shell, so switching section can stop
  // and ask instead of unmounting them silently.
  //
  // Rule 146: that report makes two claims at once -- there is something to lose, AND the
  // operator can still get to it. A state that keeps the first while dropping the second turns
  // the guard into a trap whose only exit is the destructive button. A refetch that fails after a
  // good load is exactly that state, and it is one click away: the certificate switch saves on
  // the spot and invalidates this read. React Query keeps the last good row through a failed
  // refetch, so trading the form for the "couldn't load" paragraph there would take the box and
  // its Save off screen while the typed address stayed in state, still reported unsaved.
  it("keeps the web-address box when a refetch fails, so its draft stays reachable", async () => {
    const user = userEvent.setup();
    const dirty = vi.fn();
    renderPanel([discovered(LOCAL)], dirty);

    const box = await screen.findByPlaceholderText("https://app.plex.tv");
    await user.clear(box);
    await user.type(box, "https://plex.example.org");
    await waitFor(() => expect(dirty).toHaveBeenLastCalledWith(true));

    const toggle = await screen.findByRole("switch", { name: "Check the server's certificate" });
    await waitFor(() => expect(toggle).toBeEnabled());
    apiMock.plexStatus.mockRejectedValue(new Error("server unreachable"));
    await user.click(toggle);

    await waitFor(() => expect(apiMock.plexStatus.mock.calls.length).toBeGreaterThan(1));
    // Still the form, on the last good values, with the draft still in it and still saveable.
    expect(screen.queryByText(/Couldn't load these settings/)).toBeNull();
    expect(screen.getByPlaceholderText("https://app.plex.tv")).toHaveValue(
      "https://plex.example.org",
    );
    expect(dirty).toHaveBeenLastCalledWith(true);

    // And it SAYS the read failed. Keeping the form is what makes the draft reachable; keeping
    // it with nothing said is the other half of the same mistake, because the link state, the
    // server, the libraries and the certificate switch below are then presented as current when
    // they are known to be stale (rule 17/36).
    const stale = await screen.findByText(/Couldn't check these settings just now/);
    expect(stale).toHaveClass("notice-warn");
  });

  it("stops reporting a draft once the address is saved back to the default", async () => {
    // Clearing the box is what the row's own help tells the operator to do to go back to the
    // hosted default, and it saves the empty string. The route stores that as "unset" and
    // answers with the SAME default string it was already returning, so `savedWebUrl` never
    // changes identity and the re-seed effect never fires. The box then sat empty against a
    // default it now matched: a Save button that never went away, and -- once this panel started
    // reporting upward -- a section-switch confirm no button on this panel could satisfy, for a
    // value that was already stored. Rule 39: re-seed from the server response after a save.
    const user = userEvent.setup();
    const dirty = vi.fn();
    renderPanel([discovered(LOCAL)], dirty);

    const box = await screen.findByPlaceholderText("https://app.plex.tv");
    await user.clear(box);
    await waitFor(() => expect(dirty).toHaveBeenLastCalledWith(true));

    await user.click(
      within(box.parentElement as HTMLElement).getByRole("button", { name: "Save" }),
    );

    expect(apiMock.setPlexWebUrl).toHaveBeenCalledWith("");
    await waitFor(() => expect(dirty).toHaveBeenLastCalledWith(false));
    expect(screen.getByPlaceholderText("https://app.plex.tv")).toHaveValue("https://app.plex.tv");
  });

  it("reports nothing when the manual address row is only opened", async () => {
    // The row is filled by parsing the stored address, so the two are not the same text and
    // comparing them directly called an untouched row an edit (rule 39). This address is one the
    // operator can save through this very box: `URL.hostname` lowercases the host on the way in,
    // so it comes back spelled differently than it went out. A stored scheme-default port does
    // the same thing, since `URL.port` is empty for one.
    const user = userEvent.setup();
    const dirty = vi.fn();
    apiMock.plexStatus.mockResolvedValue(
      status({ connection_uri: "https://Plex.Example.net:32400" }),
    );
    renderPanel([], dirty);

    // Settle the mount before watching anything, so what follows is only what OPENING the row
    // reported. The first data-bearing render reports one frame of `true` on its own -- the
    // web-address box is still "" while the saved value has arrived -- and the seeding effect
    // ends it in the same flush. Wait for the box to hold the saved value, which IS that frame
    // ending; waiting only for a `false` call is satisfied by the loading state's own report,
    // one turn too early, and then clears the mock right before the flash lands.
    const select = await usableConnectionSelect();
    await waitFor(() =>
      expect(screen.getByPlaceholderText("https://app.plex.tv")).toHaveValue("https://app.plex.tv"),
    );
    await waitFor(() => expect(dirty).toHaveBeenLastCalledWith(false));
    dirty.mockClear();

    await user.selectOptions(select, "__manual__");
    expect(await screen.findByText("Manual address")).toBeInTheDocument();

    // A confirm raised over a row nobody typed in is what teaches the operator to press the red
    // button without reading it, and that is the button that destroys a real draft.
    expect(dirty).not.toHaveBeenCalledWith(true);
  });

  it("reports the manual address row once its host is edited", async () => {
    // The other half: deleting `manualDirty` from the report must fail something. Without this
    // the manual row was named in three comments and pinned by no test.
    const user = userEvent.setup();
    const dirty = vi.fn();
    renderPanel([], dirty);

    await waitFor(() => expect(dirty).toHaveBeenLastCalledWith(false));
    await user.selectOptions(await usableConnectionSelect(), "__manual__");
    const host = await screen.findByPlaceholderText("plex.example.net");
    await user.clear(host);
    await user.type(host, "plex.example.org");

    await waitFor(() => expect(dirty).toHaveBeenLastCalledWith(true));
  });

  it("reports the certificate choice made before a server is linked", async () => {
    // The switch only writes once a server is linked (`if (linked)` on its onChange), so before
    // that the choice is a draft: it lives in state and rides along with the link poll. Leaving
    // the section dropped it silently, and the next sign-in then probed a self-signed server
    // with checking back on and failed with nothing on screen explaining why.
    const user = userEvent.setup();
    const dirty = vi.fn();
    apiMock.plexStatus.mockResolvedValue(
      status({ linked: false, name: null, connection_uri: null }),
    );
    renderPanel([], dirty);

    const toggle = await screen.findByRole("switch", { name: "Check the server's certificate" });
    await waitFor(() => expect(toggle).toBeEnabled());
    await waitFor(() => expect(dirty).toHaveBeenLastCalledWith(false));

    await user.click(toggle);

    expect(apiMock.setPlexWebUrl).not.toHaveBeenCalled();
    await waitFor(() => expect(dirty).toHaveBeenLastCalledWith(true));
  });

  it("reports nothing to lose once the address is put back", async () => {
    const user = userEvent.setup();
    const dirty = vi.fn();
    renderPanel([discovered(LOCAL)], dirty);

    const box = await screen.findByPlaceholderText("https://app.plex.tv");
    await user.type(box, "/extra");
    await waitFor(() => expect(dirty).toHaveBeenLastCalledWith(true));

    await user.clear(box);
    await user.type(box, "https://app.plex.tv");

    await waitFor(() => expect(dirty).toHaveBeenLastCalledWith(false));
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
