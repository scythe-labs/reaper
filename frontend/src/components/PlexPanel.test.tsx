// SPDX-License-Identifier: AGPL-3.0-or-later
// The Plex settings panel. These pin the things an operator can get stuck on: reopening the
// manual-address editor for an address they typed earlier, getting out of a sign-in whose
// plex.tv tab never opened, seeing a failed sign-in as a failure, and -- for anyone driving
// this panel by ear -- being able to tell one box on it from another.
import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { PlexResourceConnection, PlexStatus } from "../api";
import { expectNoA11yViolations } from "../test/a11y";
import { fill } from "../test/forms";
import { testQueryClient } from "../test/queryClient";
import { renderWithProviders } from "../test/renderWithProviders";
import { Announcer } from "../announce";
import { PlexPanel } from "./PlexPanel";

const { apiMock } = await vi.hoisted(async () => ({
  apiMock: (await import("../test/apiMock")).makeApiMock(),
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
  /** Status already in the cache, so the panel mounts with `data` on its FIRST render -- what
   *  every section switch back to Plex does, since `["plex"]` is read only here and stays fresh.
   *  A fresh client is a COLD mount, one render behind, which is a different code path. */
  cached?: PlexStatus,
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
  if (cached) queryClient.setQueryData(["plex"], cached);
  return renderWithProviders(
    <>
      {/* The app mounts this above every route (`App.tsx`), and `announce()` returns early when no
          region is listening -- so without it here this panel's sentences are dropped and a test
          about them passes against silence. */}
      <Announcer />
      <PlexPanel onDirtyChange={onDirtyChange} />
    </>,
    { client: queryClient },
  );
}

beforeEach(() => {
  apiMock.plexStatus.mockResolvedValue(status());
  apiMock.plexLibraries.mockResolvedValue([
    { key: 1, title: "Movies", kind: "movie", enabled: true },
  ]);
  apiMock.syncPlexLibraries.mockResolvedValue([]);
  apiMock.setPlexLibraries.mockResolvedValue([]);
  // null, not 0: the shape a fresh install actually returns (no snapshot has counted).
  apiMock.watchEvidence.mockResolvedValue({ titles: 0, held_back: null });
  apiMock.resetWatchEvidence.mockResolvedValue({ forgotten: 0 });
  // An install that has set an admin password and left deletion off -- the shipped state, and
  // the only one in which the reset is offered at all. The tests that vary it say so.
  apiMock.safety.mockResolvedValue({ destructive_enabled: false, has_password: true, note: null });
  apiMock.leavingSoonSettings.mockResolvedValue({
    enabled: false,
    allow_unarmed: false,
    last: null,
  });
  apiMock.setPlexSettings.mockResolvedValue(status());
  apiMock.plexSetConnection.mockResolvedValue(status());
  apiMock.plexLinkStart.mockResolvedValue({ pin_id: 1, auth_url: "https://plex.tv/link/pin" });
  apiMock.plexLinkPoll.mockResolvedValue({ status: "pending", server: null, servers: null });
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("the connection picker", () => {
  // Plex is where Reaper reads what has been watched, so a wrong address here starves every
  // signal that argues for keeping a file. The picker and the boxes beside it have to be
  // tellable apart by ear, which is what an operator driving this panel is doing.
  it("has no accessibility violations", async () => {
    const { container } = renderPanel();
    await usableConnectionSelect();
    await expectNoA11yViolations(container);
  });

  it("reopens the editor for an address that was typed by hand", async () => {
    const user = userEvent.setup();
    renderPanel();

    const connection = await usableConnectionSelect();
    // The typed address is its own option, and it is the one selected.
    expect(connection.value).toBe(TYPED);
    expect(screen.getByRole("option", { name: `Manual, ${TYPED}` })).toBeInTheDocument();

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
    renderWithProviders(<PlexPanel />);

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

// Every read below the connection form means "of the currently LINKED server", and not one of the
// four is qualified by a machine identifier, so a row cached against the old server answers for
// the new one. Every path that changes which server that is therefore has to refresh the whole
// set; two of them refreshed the status row alone, so unlinking and then linking a DIFFERENT
// server painted the previous server's libraries and their enabled flags -- and "Movies" and
// "TV Shows" collide across servers, so the wrong list looked like the right one (#205).
//
// **Three of the five such paths are on this panel.** The setup wizard holds the other two, and
// this file is where the claim used to be checked, which is how the wizard came to open-code a
// three-key version of the set (W10-7). The keys now live in `plexServerQueries.ts` and
// `plexServerQueries.test.ts` bans any handler from restating them; these stay because they drive
// the panel's three paths through the UI, which a source scan cannot do.
//
// **These pin the invalidation, not the symptom, and the symptom is not reachable from here.** It
// needs a cached row to still be FRESH when the query re-enables, and freshness is the one thing
// this tree does not share with production: the app sets `staleTime: 30_000` app-wide (`main.tsx`)
// while `testQueryClient` leaves it at 0, so under the suite every re-enable refetches whatever
// anyone invalidated and the grid is right either way. Reproducing it would mean giving the client
// production's staleTime, which pins a fixture as much as the panel. So each path is asserted to
// have said the whole set is no longer trusted, which is the fix, and reverting either caller to
// `["plex"]` + `["setup"]` fails here (rule 118: it does not read as a proof of the grid).
describe("changing which server is linked", () => {
  /** Every key that means "of the currently linked server". Written out rather than imported
   *  from the panel, so a key quietly dropped from `invalidateAllPlex` fails instead of moving
   *  the expectation with it (rule 119).
   *
   *  Grep-verified against the whole SPA rather than read off the panel, which is how
   *  `["plexTrash"]` came to be missing from both: it is read on the Reap page, so counting the
   *  keys declared beside the helper gives four and the tree has five. The count is pinned for
   *  the same reason -- a per-key `toContain` cannot notice a key nobody listed (rule 145). */
  const OF_THE_LINKED_SERVER = [
    ["plex"],
    ["plex-resources"],
    ["plex-libraries"],
    ["leaving-soon-settings"],
    ["plexTrash"],
    ["watch-evidence"],
  ];

  /** A mount whose invalidations the test can read back. The spy calls THROUGH, so the panel
   *  still refetches its status and the link path below reaches its linked render. */
  function renderRecordingInvalidations(): string[] {
    apiMock.plexResources.mockResolvedValue({
      source: "plex.tv",
      servers: [
        { name: "Example server", machine_identifier: "machine-1", current: true, connections: [] },
      ],
    });
    const client = testQueryClient();
    const invalidated: string[] = [];
    const passThrough = client.invalidateQueries.bind(client);
    vi.spyOn(client, "invalidateQueries").mockImplementation((filters) => {
      invalidated.push(JSON.stringify(filters?.queryKey));
      return passThrough(filters);
    });
    renderWithProviders(<PlexPanel />, { client });
    return invalidated;
  }

  function expectWholeSetDropped(invalidated: string[]) {
    for (const key of OF_THE_LINKED_SERVER) {
      expect(
        invalidated,
        `${JSON.stringify(key)} is not "of the linked server" any more`,
      ).toContain(JSON.stringify(key));
    }
    // And the SIZE of what the helper dropped, reconciled by hand against the list above. The
    // loop can only check keys somebody thought to list, so it read green for the whole life of
    // `["plexTrash"]`'s absence (rule 145). A key added to `invalidateAllPlex` and not to this
    // list fails here; `["setup"]`, which the callers invalidate separately, is not the helper's.
    const helperKeys = new Set(invalidated.filter((k) => k !== JSON.stringify(["setup"])));
    expect(
      helperKeys.size,
      `invalidateAllPlex dropped ${helperKeys.size} keys, this list names ` +
        `${OF_THE_LINKED_SERVER.length}: ${[...helperKeys].join(", ")}`,
    ).toBe(OF_THE_LINKED_SERVER.length);
  }

  it("stops trusting every row about the old server when you unlink", async () => {
    const user = userEvent.setup();
    apiMock.plexUnlink.mockResolvedValue(undefined);
    const invalidated = renderRecordingInvalidations();
    const unlink = await screen.findByRole("button", { name: "Unlink" });

    // What the panel reads back afterwards: there is no linked server now.
    apiMock.plexStatus.mockResolvedValue(
      status({ linked: false, name: null, connection_uri: null }),
    );
    await user.click(unlink);

    await waitFor(() => expect(apiMock.plexUnlink).toHaveBeenCalledTimes(1));
    await waitFor(() => expectWholeSetDropped(invalidated));
  });

  it("stops trusting them when a link lands, too", async () => {
    apiMock.plexStatus.mockResolvedValue(
      status({ linked: false, name: null, connection_uri: null }),
    );
    // The sign-in finishes on the first poll, which is the path an operator takes.
    apiMock.plexLinkPoll.mockResolvedValue({ status: "ok", server: status(), servers: null });
    const invalidated = renderRecordingInvalidations();
    const start = await screen.findByRole("button", { name: "Link with Plex" });

    // `fireEvent`, not user-event: the poll runs on a two-second interval, so this needs fake
    // timers, and user-event schedules its own on the real clock (the shape the timed-out test
    // above uses).
    vi.useFakeTimers();
    try {
      fireEvent.click(start);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0); // the PIN request settles
        await vi.advanceTimersByTimeAsync(2100); // the first poll, which comes back linked
      });
    } finally {
      vi.useRealTimers();
    }

    expectWholeSetDropped(invalidated);
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
    renderWithProviders(<PlexPanel />);

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
    await fill(user, box, "https://plex.example.org");
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

    expect(apiMock.setPlexSettings).toHaveBeenCalledWith({ web_url: "" });
    await waitFor(() => expect(dirty).toHaveBeenLastCalledWith(false));
    expect(screen.getByPlaceholderText("https://app.plex.tv")).toHaveValue("https://app.plex.tv");
  });

  it("never reports a draft for the frame before its boxes are seeded", async () => {
    // #139's twin on this panel (rule 72). Same defect, different guard: this panel re-seeds on
    // every change of the stored value where `GeneralPanel` seeds once, so it asks which value
    // its boxes were seeded FROM rather than merely whether they have been -- a save or another
    // tab moving the address opens the same window again, and a "have we seeded" flag would
    // miss that second one entirely.
    //
    // The web address is the reachable one: the fixture stores a non-empty address, so the box
    // starts "" against it. Deterministic because the report is an effect, so the spy records
    // the call whether or not the frame was painted.
    const dirty = vi.fn();
    renderPanel([], dirty);

    await waitFor(() =>
      expect(screen.getByPlaceholderText("https://app.plex.tv")).toHaveValue("https://app.plex.tv"),
    );

    expect(dirty).not.toHaveBeenCalledWith(true);
  });

  it("never reports a draft when the stored status is already cached", async () => {
    // The warm half of the test above, and the one the guard is actually for. A fresh
    // `QueryClient` mounts this panel COLD: `data` is undefined on the first render, so the box
    // and the stored value are both "" and no comparison can go wrong yet. Every render after
    // the first section switch is warm instead -- `["plex"]` is read only by this panel, nothing
    // evicts it inside the default window, and `Settings` unmounts and remounts the panel per
    // section -- so `data` is there on the first render with the box still "". A guard seeded
    // from `savedWebUrl` agrees with it on that frame and passes, which is #139 surviving on the
    // twin (rule 72); the confirm built on this report then names Plex settings nobody typed
    // (rule 146). Cold and warm are one line apart and only one of them fails.
    const dirty = vi.fn();
    renderPanel([], dirty, status());

    await waitFor(() =>
      expect(screen.getByPlaceholderText("https://app.plex.tv")).toHaveValue("https://app.plex.tv"),
    );

    expect(dirty).not.toHaveBeenCalledWith(true);
  });

  it("never reports a draft for a cached certificate choice either", async () => {
    // The certificate switch carries the same guard as the address box and is fixed with it
    // (rule 72), so it gets the same warm mount. What it does NOT get is a claim of live
    // exposure: today the status read omits the certificate flag while unlinked and the schema
    // default is on, so the stored value and this box's initial value are both `true` on every
    // real server and no operator can reach the frame this pins. The state below is therefore
    // one the API does not currently produce, and the test says so rather than reading as a
    // reproduction (rule 118). It pins the SHAPE of the guard: that the flag is a sentinel no
    // stored value can equal, so nobody rewrites it as one seeded from the stored value and
    // finds out later, when the route starts sending the flag, that it was inert all along.
    //
    // The address is stored empty here so the box and its saved value agree, leaving the
    // certificate switch as the only thing that can report anything at all.
    const unlinked = status({ linked: false, verify_tls: false, web_url: "" });
    apiMock.plexStatus.mockResolvedValue(unlinked);
    const dirty = vi.fn();
    renderPanel([], dirty, unlinked);

    await waitFor(() =>
      expect(screen.getByLabelText("Check the server's certificate")).not.toBeChecked(),
    );

    expect(dirty).not.toHaveBeenCalledWith(true);
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
    // reported. Waiting only for a `false` call is satisfied by the loading state's own report,
    // one turn too early. The mount used to report one frame of `true` on its own here, the
    // web-address box still "" while the saved value had arrived; that was #139 and the panel
    // no longer does it, which the test above pins. The wait stays because a settled mount is
    // still the right starting point for a test about opening the row.
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
    await fill(user, host, "plex.example.org");

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

    expect(apiMock.setPlexSettings).not.toHaveBeenCalled();
    await waitFor(() => expect(dirty).toHaveBeenLastCalledWith(true));
  });

  it("reports nothing to lose once the address is put back", async () => {
    const user = userEvent.setup();
    const dirty = vi.fn();
    renderPanel([discovered(LOCAL)], dirty);

    const box = await screen.findByPlaceholderText("https://app.plex.tv");
    await user.type(box, "/extra");
    await waitFor(() => expect(dirty).toHaveBeenLastCalledWith(true));

    await fill(user, box, "https://app.plex.tv");

    await waitFor(() => expect(dirty).toHaveBeenLastCalledWith(false));
  });
});

describe("the certificate check", () => {
  it("warns beside the switch that turned it off", async () => {
    const user = userEvent.setup();
    // The saved value follows the server, so the save has to answer with the new one or
    // the refetch would flip the switch straight back on.
    apiMock.setPlexSettings.mockImplementation(
      async (patch: { web_url?: string; verify_tls?: boolean }) => {
        const next = status({ verify_tls: patch.verify_tls ?? true });
        apiMock.plexStatus.mockResolvedValue(next);
        return next;
      },
    );
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

  it("sends the certificate check and nothing else", async () => {
    // It used to send the saved web address beside it, read from `["plex"]`'s cached row, and the
    // route wrote every field it received. So a cached row that had gone out of date -- a failed
    // refetch this panel deliberately renders through, or another tab editing the address --
    // reverted the operator's address without a word, and every "open in Plex" link in the app
    // then pointed at plex.tv. `web_url` is what must be ABSENT, so this asserts the exact
    // payload rather than the switch's own value, which was never the part that was wrong (#204).
    const user = userEvent.setup();
    apiMock.setPlexSettings.mockResolvedValue(status({ verify_tls: false }));
    renderPanel();

    const toggle = await screen.findByRole("switch", { name: "Check the server's certificate" });
    await user.click(toggle);

    await waitFor(() => expect(apiMock.setPlexSettings).toHaveBeenCalledTimes(1));
    expect(apiMock.setPlexSettings).toHaveBeenCalledWith({ verify_tls: false });
    const [patch] = apiMock.setPlexSettings.mock.calls[0] as [Record<string, unknown>];
    expect(Object.keys(patch), "the address is not this control's to write").toEqual([
      "verify_tls",
    ]);
  });
});

// The three groups below the connection form, driven through a failed refetch (#166).
//
// The panel's own status read got this split in #140; these two did not, so an undivided
// `isError` traded the whole library grid, and separately both Leaving Soon switches, for one
// error paragraph while React Query still held the last good answer. Every trigger is a success
// path: `invalidateAllPlex` fires on all three paths that change which server is linked -- a
// switch, a link and an unlink -- and returning to the section past `staleTime` refetches on its
// own. For most of this comment's life the switch was its only caller and the other two refreshed
// the status row alone, which is the bug #205 fixed; the count here has been wrong in both
// directions since, so the callers are now pinned by name in "changing which server is linked"
// below rather than counted in prose. Each group is pinned in both directions, because a fix that
// only deleted the `isError` arm would leave a genuinely-unread group claiming to be empty rather than unread.
describe("the groups below the form, through a failed refetch", () => {
  /** A cold mount whose queryClient the test keeps, so it can invalidate one key by hand. */
  function renderWithClient() {
    apiMock.plexResources.mockResolvedValue({
      source: "plex.tv",
      servers: [
        { name: "Example server", machine_identifier: "machine-1", current: true, connections: [] },
      ],
    });
    const queryClient = testQueryClient();
    renderWithProviders(<PlexPanel />, { client: queryClient });
    return queryClient;
  }

  const NEVER_LOADED_LIBS = /Couldn't load the library list/;
  const NEVER_LOADED_SHELF = /Couldn't load the Leaving Soon settings/;
  const NEVER_LOADED_WATCH = /Couldn't load the watch history record/;
  const STALE_LIBS = /Couldn't check the library list just now/;
  const STALE_SHELF = /Couldn't check the Leaving Soon settings just now/;
  const STALE_WATCH = /Couldn't check the watch history record just now/;
  // The one this panel speaks in once several of its reads have failed at once (#198).
  const STALE_PANEL = /Couldn't check the Plex settings just now/;
  const STALE_ANY = /Couldn't check .* just now/;
  // Rule 144: the noun is the `what` prop of StaleReadNotice, which owns the sentence. Deleting
  // either `what` here leaves the loose form below green, so each is asserted by its own noun.
  const WHAT_HINT =
    "The stale line's noun is the `what` prop of StaleReadNotice.tsx, which owns the sentence. " +
    'Sibling call sites: PlexPanel\'s own status read (the default, "these settings"), the ' +
    'library grid ("the library list"), the Leaving Soon group ("the Leaving Soon settings"); ' +
    "AboutPanel.tsx, JobsPanel.tsx (the panel and LeavingSoonRow), NotificationsPanel.tsx and " +
    "ServicesPanel.tsx.";

  it("keeps the library grid and its switches when the refetch fails", async () => {
    const queryClient = renderWithClient();
    // Cheap text first, then the switch synchronously (#228, rule 72's twin of the Leaving Soon
    // row in SettingsStaleRead.test.tsx). This grid is two reads deep -- the panel returns early
    // while `plex` is pending and the grid waits on `plex-libraries` behind it -- and a
    // `findByRole` with a name matcher re-computes accessible names across the whole panel on
    // every 50ms poll, so the pair had one 1000ms budget with the looking taken out of it.
    await screen.findByText("Refresh libraries");
    const toggle = screen.getByRole("switch", { name: "Let Reaper touch Movies" });

    apiMock.plexLibraries.mockRejectedValue(new Error("boom"));
    await act(async () => {
      await queryClient.invalidateQueries({ queryKey: ["plex-libraries"] });
    });
    await waitFor(() => expect(apiMock.plexLibraries).toHaveBeenCalledTimes(2));

    const stale = await screen.findByText(STALE_LIBS);
    expect(stale, WHAT_HINT).toHaveTextContent(STALE_LIBS);
    expect(stale).toHaveClass("notice-warn");
    // The claim that matters: the grid the sentence talks over is still drawn, and the switch is
    // still operable. A library an operator cannot see is one they cannot turn off.
    expect(screen.queryByText(NEVER_LOADED_LIBS)).toBeNull();
    expect(screen.getByRole("switch", { name: "Let Reaper touch Movies" })).toBe(toggle);
    expect(toggle).toBeEnabled();
    expect(screen.getByRole("button", { name: "Refresh libraries" })).toBeEnabled();
  });

  it("says it once when several reads fail together", async () => {
    // The state `invalidateAllPlex` produces: one server switch against an unreachable Plex
    // refetches all four of these, and each used to answer with its own amber paragraph, so the
    // operator read the same failure four times down one panel (#198). The notice carries
    // role="alert", so it was four announcements as well.
    const queryClient = renderWithClient();
    await screen.findByText("Refresh libraries");
    await screen.findByRole("button", { name: "Forget…" });

    apiMock.plexLibraries.mockRejectedValue(new Error("boom"));
    apiMock.watchEvidence.mockRejectedValue(new Error("boom"));
    await act(async () => {
      await queryClient.invalidateQueries({ queryKey: ["plex-libraries"] });
      await queryClient.invalidateQueries({ queryKey: ["watch-evidence"] });
    });
    await waitFor(() => expect(apiMock.plexLibraries).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(apiMock.watchEvidence).toHaveBeenCalledTimes(2));

    const lines = await screen.findAllByText(STALE_ANY);
    expect(lines, WHAT_HINT).toHaveLength(1);
    expect(lines[0]).toHaveTextContent(STALE_PANEL);
    // Neither read speaks for itself while the panel is speaking for both.
    expect(screen.queryByText(STALE_LIBS)).toBeNull();
    expect(screen.queryByText(STALE_WATCH)).toBeNull();
    // And the line still sits above everything it now covers, since it says what's BELOW may be
    // out of date and `.panel` is plain block flow.
    const grid = screen.getByRole("switch", { name: "Let Reaper touch Movies" });
    expect(lines[0]!.compareDocumentPosition(grid) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    // The controls under it are all still drawn and operable, which is why the panel keeps
    // its surface through a failed refetch.
    expect(grid).toBeEnabled();
    expect(screen.getByRole("button", { name: "Forget…" })).toBeEnabled();
  });

  it("keeps the watch-history control when the refetch fails", async () => {
    const queryClient = renderWithClient();
    await screen.findByRole("button", { name: "Forget…" });

    apiMock.watchEvidence.mockRejectedValue(new Error("boom"));
    await act(async () => {
      await queryClient.invalidateQueries({ queryKey: ["watch-evidence"] });
    });
    await waitFor(() => expect(apiMock.watchEvidence).toHaveBeenCalledTimes(2));

    const stale = await screen.findByText(STALE_WATCH);
    expect(stale, WHAT_HINT).toHaveTextContent(STALE_WATCH);
    expect(stale).toHaveClass("notice-warn");
    // The claim that matters: this is the way out of a library-wide hold, so a failed refetch
    // must not take it off screen while the last good answer is still in hand.
    expect(screen.queryByText(NEVER_LOADED_WATCH)).toBeNull();
    expect(screen.getByRole("button", { name: "Forget…" })).toBeEnabled();
  });

  it("still says the watch record never loaded when the first read is the one that fails", async () => {
    apiMock.watchEvidence.mockRejectedValue(new Error("boom"));
    renderWithClient();

    expect(await screen.findByText(NEVER_LOADED_WATCH)).toBeInTheDocument();
    // Nothing was ever held, so there is no control for the stale line to be about, and offering
    // a reset whose current state we could not read would be a button with no stated effect.
    expect(screen.queryByText(STALE_WATCH)).toBeNull();
    expect(screen.queryByRole("button", { name: "Forget…" })).toBeNull();
  });

  it("still says the library list never loaded when the first read is the one that fails", async () => {
    apiMock.plexLibraries.mockRejectedValue(new Error("boom"));
    renderWithClient();

    expect(await screen.findByText(NEVER_LOADED_LIBS)).toBeInTheDocument();
    // Nothing was ever held, so there is no grid for the stale line to be about.
    expect(screen.queryByText(STALE_LIBS)).toBeNull();
    expect(screen.queryByRole("switch", { name: "Let Reaper touch Movies" })).toBeNull();
  });

  it("keeps both Leaving Soon switches when the refetch fails", async () => {
    const queryClient = renderWithClient();
    // Same shape as the grid above, on the same query key #228 was about. "Update while
    // read-only" is the second row's label, rendered only once `leaving-soon-settings` has
    // landed, so it settles the read this switch's existence depends on without walking the
    // tree for names.
    await screen.findByText("Update while read-only");
    const shelf = screen.getByRole("switch", { name: 'Show "Leaving Soon" in Plex' });

    apiMock.leavingSoonSettings.mockRejectedValue(new Error("boom"));
    await act(async () => {
      await queryClient.invalidateQueries({ queryKey: ["leaving-soon-settings"] });
    });
    await waitFor(() => expect(apiMock.leavingSoonSettings).toHaveBeenCalledTimes(2));

    const stale = await screen.findByText(STALE_SHELF);
    expect(stale, WHAT_HINT).toHaveTextContent(STALE_SHELF);
    expect(stale).toHaveClass("notice-warn");
    expect(screen.queryByText(NEVER_LOADED_SHELF)).toBeNull();
    // Including the one that decides whether Reaper writes to Plex before deletion is armed.
    expect(screen.getByRole("switch", { name: 'Show "Leaving Soon" in Plex' })).toBe(shelf);
    expect(screen.getByRole("switch", { name: "Update while read-only" })).toBeEnabled();
  });

  it("still says the shelf settings never loaded when the first read is the one that fails", async () => {
    apiMock.leavingSoonSettings.mockRejectedValue(new Error("boom"));
    renderWithClient();

    expect(await screen.findByText(NEVER_LOADED_SHELF)).toBeInTheDocument();
    expect(screen.queryByText(STALE_SHELF)).toBeNull();
    expect(screen.queryByRole("switch", { name: 'Show "Leaving Soon" in Plex' })).toBeNull();
  });
});

// The watch-record reset. A title whose measured plays fall to zero reads as
// unreadable, which is right until the cause is a rebuilt library -- then it is every
// watched title at once and nothing is reapable. This is the way out, so it has to be
// reachable, has to say what it will do, and must not be reachable by one stray click.
describe("forgetting the recorded watch history", () => {
  function renderPanel() {
    apiMock.plexResources.mockResolvedValue({
      source: "plex.tv",
      servers: [
        { name: "Example server", machine_identifier: "machine-1", current: true, connections: [] },
      ],
    });
    const queryClient = testQueryClient();
    renderWithProviders(
      <>
        {/* `announce()` returns early when no region is listening, so without this the
            sentence below is dropped and the test passes against silence. */}
        <Announcer />
        <PlexPanel />
      </>,
      { client: queryClient },
    );
    return queryClient;
  }

  const spoken = () =>
    [...document.querySelectorAll('[aria-live="polite"]')].map((n) => n.textContent).join("");

  const PASSWORD = "correct-horse-battery";

  /** Open the reset and type the admin password into the confirm form.
   *
   *  Rule 137: the Confirm is disabled until the box has something in it, and user-event reports
   *  a click on a disabled control as SUCCESS -- so a test that pressed it before typing would
   *  send nothing, then fail later on a state its no-op never produced. */
  async function arm(user: ReturnType<typeof userEvent.setup>, password = PASSWORD) {
    await user.click(await screen.findByRole("button", { name: "Forget…" }));
    const box = screen.getByLabelText("Admin password");
    if (password) await fill(user, box, password);
    return box;
  }

  it("takes two presses and a password, and sends nothing on the first", async () => {
    const user = userEvent.setup();
    apiMock.watchEvidence.mockResolvedValue({ titles: 1284, held_back: 3 });
    renderPanel();

    const open = await screen.findByRole("button", { name: "Forget…" });
    await user.click(open);

    // Armed, and still nothing sent: the second press is the one that sends.
    expect(apiMock.resetWatchEvidence).not.toHaveBeenCalled();
    const confirm = screen.getByRole("button", { name: "Confirm forget" });
    expect(screen.queryByRole("button", { name: "Forget…" })).toBeNull();
    // Nor can the second press land on its own: an empty box confirms nothing.
    expect(confirm).toBeDisabled();

    await fill(user, screen.getByLabelText("Admin password"), PASSWORD);
    await waitFor(() => expect(confirm).toBeEnabled());
    await user.click(confirm);
    // The password reaches the server, which is the whole of this change: the route refuses
    // without it, so a call that dropped it on the floor would read as green here and 403 in
    // the app.
    // Read off the first argument rather than matching the whole call: React Query hands a
    // mutationFn its own context as a second argument, which the client ignores and this
    // assertion has no business pinning.
    expect(apiMock.resetWatchEvidence).toHaveBeenCalledTimes(1);
    expect(apiMock.resetWatchEvidence.mock.calls[0]?.[0]).toBe(PASSWORD);
  });

  it("stands down on Cancel, sending nothing and keeping no password", async () => {
    const user = userEvent.setup();
    renderPanel();

    await arm(user);
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(apiMock.resetWatchEvidence).not.toHaveBeenCalled();
    expect(await screen.findByRole("button", { name: "Forget…" })).toBeEnabled();

    // Reopening offers an EMPTY box. Cancel that only closed the form would leave the admin
    // password in component state for as long as this panel stayed mounted, and refill the field
    // on the next open -- the shape S-5 was filed for.
    await user.click(screen.getByRole("button", { name: "Forget…" }));
    expect(screen.getByLabelText("Admin password")).toHaveValue("");
  });

  it("says how many it forgot, and when that takes effect", async () => {
    const user = userEvent.setup();
    apiMock.watchEvidence.mockResolvedValue({ titles: 1284, held_back: 3 });
    apiMock.resetWatchEvidence.mockResolvedValue({ forgotten: 1284 });
    renderPanel();

    await arm(user);
    await user.click(screen.getByRole("button", { name: "Confirm forget" }));

    // The second sentence is load-bearing: the stored candidates are frozen snapshot data, so
    // the queue does not move until the next scan. Without it the operator reads the unchanged
    // queue as "that did not work" and reaches for the policy next, which does cost files.
    //
    // Scoped to the visible row, because this sentence is on screen TWICE on purpose -- the live
    // region says the same words (rule 144, pinned separately below), and an unscoped match
    // finds both and throws.
    const status = await screen.findByText(/Forgotten for 1,284 titles/, {
      selector: ".set-status",
    });
    expect(status).toHaveTextContent("The next scan uses what Plex holds now.");
  });

  // The fallback, for a failure that arrived with nothing to say. Without it the operator gets a
  // red box holding no words, which is worse than the fixed sentence this used to always show:
  // "nothing changed" is the load-bearing half, since someone reading a bare failure cannot tell
  // whether pressing again is safe.
  it("still says nothing changed when the failure carries no message", async () => {
    const user = userEvent.setup();
    apiMock.resetWatchEvidence.mockRejectedValue(new Error(""));
    renderPanel();

    await arm(user);
    await user.click(screen.getByRole("button", { name: "Confirm forget" }));

    const failure = await screen.findByText(/Couldn't forget the record/);
    expect(failure).toHaveClass("notice-error");
    expect(failure).toHaveTextContent("Nothing changed.");
  });

  // Every refusal the password gate can raise needs a different move from the operator, so the
  // server's own sentence is what gets rendered. A fixed "couldn't forget the record" would have
  // told someone locked out to keep pressing.
  it.each([
    "That password didn't match. The record was kept.",
    "Too many attempts. Please wait and try again.",
    "The server is busy checking passwords. Please try again shortly.",
  ])("shows the server's own refusal: %s", async (detail) => {
    const user = userEvent.setup();
    apiMock.resetWatchEvidence.mockRejectedValue(new Error(detail));
    renderPanel();

    await arm(user);
    await user.click(screen.getByRole("button", { name: "Confirm forget" }));

    expect(await screen.findByText(detail)).toHaveClass("notice-error");
  });

  it("drops the typed password when the server refuses it", async () => {
    const user = userEvent.setup();
    apiMock.resetWatchEvidence.mockRejectedValue(new Error("That password didn't match."));
    renderPanel();

    await arm(user);
    await user.click(screen.getByRole("button", { name: "Confirm forget" }));
    await screen.findByText("That password didn't match.");

    // The form stays open to retype into, but the wrong password does not sit there waiting to
    // be resent -- the same thing `RestoreCard` does with a rejected confirm.
    expect(screen.getByLabelText("Admin password")).toHaveValue("");
  });

  // The three states that are not "a password exists" all fail closed: this control withdraws a
  // protection from every title at once, so there is no direction here that is safe to offer on
  // a guess. `DeletionToggle` keeps its OFF direction live on an unreadable safety read because
  // that one can only make Reaper safer; this row has no such half.
  // Both exits unmount the form and take the pressed button with them, so focus falls to
  // `<body>` and the next Tab restarts at the top of the page. The successor is the button that
  // opened the form, which is back in that slot by the next commit. Same handoff the two Saves
  // on this panel make, through the same hook (rule 72).
  it.each([
    ["Confirm forget", "success"],
    ["Cancel", "standing down"],
  ])("hands focus back to Forget… after %s", async (press) => {
    const user = userEvent.setup();
    apiMock.resetWatchEvidence.mockResolvedValue({ forgotten: 3 });
    renderPanel();

    await arm(user);
    await user.click(screen.getByRole("button", { name: press }));

    const reopen = await screen.findByRole("button", { name: "Forget…" });
    await waitFor(() => expect(reopen).toHaveFocus());
  });

  it("says how many it forgot out loud, not only on screen", async () => {
    const user = userEvent.setup();
    apiMock.resetWatchEvidence.mockResolvedValue({ forgotten: 1284 });
    renderPanel();

    await arm(user);
    await user.click(screen.getByRole("button", { name: "Confirm forget" }));

    // The status line is the only thing that moves, and it sits in an unfocused subtree. Reads
    // the same as the visible sentence (rule 144).
    await waitFor(() =>
      expect(spoken()).toContain(
        "Forgotten for 1,284 titles. The next scan uses what Plex holds now.",
      ),
    );
  });

  it("offers nothing to press until an admin password is set", async () => {
    apiMock.safety.mockResolvedValue({
      destructive_enabled: false,
      has_password: false,
      note: null,
    });
    renderPanel();

    expect(await screen.findByText(/Set an admin password first/)).toHaveTextContent(
      "Settings → Security",
    );
    expect(screen.queryByRole("button", { name: "Forget…" })).toBeNull();
    expect(apiMock.resetWatchEvidence).not.toHaveBeenCalled();
  });

  it("offers nothing to press when it can't tell whether a password is set", async () => {
    apiMock.safety.mockRejectedValue(new Error("boom"));
    renderPanel();

    // Named, not blank: a control that simply vanished would read as "this install doesn't have
    // that feature" rather than "Reaper couldn't look" (rule 17/36).
    await screen.findByText(/couldn't check whether an admin password is set/);
    expect(screen.queryByRole("button", { name: "Forget…" })).toBeNull();
    expect(apiMock.resetWatchEvidence).not.toHaveBeenCalled();
  });

  // The status line, whose job is to answer "do I need to press this at all". Three readings,
  // and the null one is the reason this is a table: a scan that never counted is not a scan that
  // counted none (rule 93), so it must not claim the last scan found nothing unreadable.
  it.each([
    [
      { titles: 1284, held_back: 3 },
      "Holding a record for 1,284 titles. The last scan couldn't read the plays for 3 items.",
    ],
    [
      { titles: 1284, held_back: 1 },
      "Holding a record for 1,284 titles. The last scan couldn't read the plays for 1 item.",
    ],
    [
      { titles: 1284, held_back: 0 },
      "Holding a record for 1,284 titles. The last scan found no unreadable plays.",
    ],
    [{ titles: 1284, held_back: null }, "Holding a record for 1,284 titles."],
    [
      { titles: 1, held_back: 0 },
      "Holding a record for 1 title. The last scan found no unreadable plays.",
    ],
  ])("reads %o as its own sentence", async (evidence, expected) => {
    apiMock.watchEvidence.mockResolvedValue(evidence);
    renderPanel();
    expect(await screen.findByText(expected)).toBeInTheDocument();
  });

  // "Held back" is this app's phrase for an item with no readable SIZE, in the planner and on
  // four docs pages, where the repair is a policy allowance. Reusing it for unreadable plays
  // would send the operator to that allowance instead of to Tautulli (rules 21, 144).
  it("does not describe an unreadable-history item with the unknown-size phrase", async () => {
    apiMock.watchEvidence.mockResolvedValue({ titles: 1284, held_back: 3 });
    renderPanel();
    const status = await screen.findByText(/couldn't read the plays for 3 items/);
    expect(status).not.toHaveTextContent("held back");
    // Nor a verdict claim: the number is not computed from one (rule 144).
    expect(status).not.toHaveTextContent("kept");
  });

  it("warns that a watched title will score as unwatched until Tautulli is repaired", async () => {
    renderPanel();
    // The honest cost of pressing it. A reset that reads as free is the one an operator presses
    // without repairing the source, and then every re-added title is condemnable on false zeros.
    const warning = await screen.findByText(/score as never watched/);
    expect(warning).toHaveClass("notice-warn");
    expect(warning).toHaveTextContent("repair its history in Tautulli");
  });
});

describe("saving the Plex web address", () => {
  // The row's inline Save exists only while the box is dirty, so pressing it destroys the pressed
  // control, and it is `disabled` while the write is in flight so focus is gone before that. Its
  // whole success signal was that disappearance, which is an absence and cannot be heard -- while
  // the Manual address row beside it has said "Connection saved." all along, so the two halves of
  // one pair disagreed (#173, rule 72).
  const spoken = () =>
    [...document.querySelectorAll('[aria-live="polite"]')].map((n) => n.textContent).join("");
  const box = () => screen.getByLabelText("Plex web address");

  async function typeAndSave() {
    const user = userEvent.setup();
    renderPanel();
    const address = await waitFor(() => box());
    await user.type(address, "x");
    const save = await screen.findByRole("button", { name: "Save" });
    await waitFor(() => expect(save).toBeEnabled());
    await user.click(save);
    return { user, address };
  }

  it("says the address was saved", async () => {
    await typeAndSave();

    await waitFor(() => expect(spoken()).toContain("Plex web address saved."));
  });

  it("hands focus back to the box, which is where the operator was working", async () => {
    // The box outlives its own Save and still holds the value just committed, so it is both the
    // stable neighbour and the thing the operator was looking at. There is no heading on this row
    // to fall back to.
    const { address } = await typeAndSave();

    await waitFor(() => expect(address).toHaveFocus());
  });
});
