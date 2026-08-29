// SPDX-License-Identifier: AGPL-3.0-or-later
// The Plex settings panel. These tests pin the mistakes an operator can get stuck on:
// reopening the manual-address editor for an address typed earlier, getting out of a sign-in
// whose plex.tv tab never opened, showing a failed sign-in as a failure, and letting someone
// driving this panel by ear tell one box on it from another.
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

// jsdom has no window.open. Any test that reaches "Link with Plex" prints "Not implemented:
// Window's open() method" into the CI log without this stub. The stub also lets this test check
// for the noopener feature string, which Login.test.tsx and SetupPlexStep.test.tsx check for
// their own copies of this popup. Without noopener, plex.tv would get a handle on the page it
// opened from and could navigate it.
const opened = vi.fn<typeof window.open>(() => null);
beforeEach(() => vi.stubGlobal("open", opened));
afterEach(() => {
  opened.mockClear();
  vi.unstubAllGlobals();
});

const { apiMock } = await vi.hoisted(async () => ({
  apiMock: (await import("../test/apiMock")).makeApiMock(),
}));

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  api: apiMock,
}));

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
 *  Its options come from the fast, local status read, so the picker appears on screen before it
 *  works: it stays disabled while the lookup runs. user-event treats a click on a disabled
 *  select as success, so a test that acts before the select is enabled does nothing and then
 *  fails later on an editor that never opened. Wait for the select to become enabled first. */
async function usableConnectionSelect(): Promise<HTMLSelectElement> {
  await waitFor(() => expect(connectionSelect()).toBeEnabled());
  return connectionSelect();
}

function renderPanel(
  connections: PlexResourceConnection[] = [discovered(LOCAL)],
  /** Stable across renders, which the prop requires: pass one `vi.fn()`, never an inline arrow. */
  onDirtyChange?: (dirty: boolean) => void,
  /** Status already in the cache, so the panel mounts with `data` set on its first render. This
   *  is what happens every time an operator switches back to the Plex section, since `["plex"]`
   *  is read only here and stays fresh. A fresh client mounts cold instead, one render behind,
   *  which is a different code path. */
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
      {/* The app mounts this above every route in `App.tsx`. `announce()` returns early when no
          region is listening, so without it here, this panel's spoken sentences are dropped and
          a test about them would pass against silence. */}
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
  // A fresh install returns null, not 0, since no snapshot has counted yet.
  apiMock.watchEvidence.mockResolvedValue({ titles: 0, held_back: null });
  apiMock.resetWatchEvidence.mockResolvedValue({ forgotten: 0 });
  // The shipped state: an admin password is set and deletion is off. The reset is offered only
  // in this state; tests that use a different state say so explicitly.
  apiMock.safety.mockResolvedValue({
    destructive_enabled: false,
    has_password: true,
  });
  apiMock.leavingSoonSettings.mockResolvedValue({
    enabled: false,
    allow_unarmed: false,
    name: "Leaving Soon",
    applied_name: "Leaving Soon",
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
  // signal that argues for keeping a file. The picker and the boxes beside it have to be told
  // apart by ear, which is what an operator driving this panel is doing.
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
  // A `.set-row` label is a plain `<span className="set-label">`, which is not an accessible
  // name. Rows built on a Switch get a name because Switch takes an `ariaLabel` and every one
  // passes it. Rows built on a plain box had no accessible name at all, so a screen reader
  // announced the server picker, the connection picker, and the web address as three identical
  // bare fields. The connection picker is what tells Reaper which server to manage.
  //
  // This test counts every named control instead of only checking that the controls it finds
  // have a name, because a query that misses a control would still pass under the weaker check.
  // The pickers are found through `getByRole("option").closest("select")`, a query that never
  // asks for a name on its own. The table below lists every input and select this fixture
  // renders, checked by hand against the source; the count catches a control this query would
  // otherwise skip silently. The check only covers the branches this fixture actually mounts: a
  // control added to a branch nothing here renders would be missing from the table and from the
  // count, so it would still pass. The branches not covered are `resources.isError`,
  // `linkedServerMissing`, the unlinked `ServerPickList`, and the pending and `!data` arms; none
  // renders a control today, and none is watched by this test if that changes.
  it("answers to the label the operator can see", async () => {
    const user = userEvent.setup();
    const { container } = renderPanel();

    // The manual editor starts closed, and its two boxes are part of the set counted below, so
    // open it.
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
      ["Shelf name", "input"],
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
    // If `currentServer` fell back to `servers[0]`, a partial or filtered plex.tv response
    // would silently promote a different server to "the one Reaper manages," and the Connection
    // row would list that server's addresses. Saving would then point Reaper's Leaving Soon
    // writes and its Never-Reap read at a library it was never linked to.
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

    // The other server never appears as an option, not even one a browser would render. A
    // select whose value matches nothing shows its first option, so listing the other server
    // would still read as "this is your server," just unsavable. The box names the linked
    // server instead, and neither picker can be used.
    expect(screen.queryByRole("option", { name: "Someone else's server" })).not.toBeInTheDocument();
    const server = screen.getByRole("option", { name: "Example server" }).closest("select");
    expect(server).toBeDisabled();
    expect(connectionSelect()).toBeDisabled();

    // The other server's addresses are not offered either.
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
    expect(opened).toHaveBeenCalled();
    expect(opened.mock.calls[0]?.[2] ?? "").toContain("noopener");

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

// Every read below the connection form answers for "the currently linked server," and none of
// the four is scoped by a machine identifier. A row cached from the old server would answer for
// the new one unless every path that changes the linked server refreshes the whole set. If a
// path refreshed only the status row, unlinking and then linking a different server would show
// the previous server's libraries and their enabled flags, since "Movies" and "TV Shows" collide
// across servers and the wrong list looks like the right one.
//
// Three of the five paths that change the linked server are on this panel; the setup wizard
// holds the other two. The keys live in `plexServerQueries.ts`, and `plexServerQueries.test.ts`
// bans any handler from restating them. These tests stay here because they drive the panel's
// three paths through the UI, which a source scan cannot do.
//
// These tests pin the cache invalidation, not the visible symptom, because the symptom needs a
// cached row that is still fresh when the query re-enables, and this test tree does not share
// freshness with production. The app sets `staleTime: 30_000` everywhere (`main.tsx`), but
// `testQueryClient` leaves it at 0, so every re-enable in this suite refetches whatever was
// invalidated and the grid ends up correct either way. So each path is checked for reporting the
// whole set as no longer trusted, which is the actual fix; reverting either caller to
// `["plex"]` + `["setup"]` still fails this test even though it would not reproduce the visible
// bug.
describe("changing which server is linked", () => {
  /** Every key that means "of the currently linked server." Written out here instead of
   *  imported from the panel, so a key quietly dropped from `invalidateAllPlex` fails this test
   *  instead of changing the expectation along with it.
   *
   *  Checked against the whole app with a text search rather than read off the panel alone: a
   *  key used only elsewhere, such as `["plexTrash"]` on the Reap page, would otherwise be
   *  missed. The count below is pinned too, since checking only that each listed key appears
   *  cannot notice a key nobody listed. */
  const OF_THE_LINKED_SERVER = [
    ["plex"],
    ["plex-resources"],
    ["plex-libraries"],
    ["leaving-soon-settings"],
    ["plexTrash"],
    ["watch-evidence"],
  ];

  /** A mount whose invalidations the test can read back. The spy calls through, so the panel
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
    // This also checks the SIZE of what the helper dropped, reconciled by hand against the list
    // above. The loop above only checks keys someone listed, so it cannot notice a key that is
    // missing from the list entirely, such as `["plexTrash"]`. A key added to `invalidateAllPlex`
    // and not to this list fails here. `["setup"]` is not the helper's key: the callers
    // invalidate it separately.
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

    // This uses `fireEvent`, not user-event, because the poll runs on a two-second interval and
    // needs fake timers. user-event schedules its own timers on the real clock, which the
    // timed-out test above relies on instead.
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
  // That report makes two claims at once: there is something to lose, and the operator can
  // still reach it to save or discard it. A state that keeps the first claim while dropping the
  // second turns the guard into a trap whose only way out is the destructive button. A refetch
  // that fails after a good load is exactly that state, and it is one click away: the
  // certificate switch saves immediately and invalidates this read. React Query keeps the last
  // good row through a failed refetch, so replacing the form with a "couldn't load" paragraph
  // would take the box and its Save button off screen while the typed address stayed in state,
  // still reported as unsaved.
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
    // The form is still shown, with the last good values, the draft still in it, and still
    // saveable.
    expect(screen.queryByText(/Couldn't load these settings/)).toBeNull();
    expect(screen.getByPlaceholderText("https://app.plex.tv")).toHaveValue(
      "https://plex.example.org",
    );
    expect(dirty).toHaveBeenLastCalledWith(true);

    // The panel also says the read failed. Keeping the form is what makes the draft reachable,
    // but keeping it silent would be the other half of the same mistake: the link state,
    // server, libraries, and certificate switch below would then look current while known to be
    // stale.
    const stale = await screen.findByText(/Couldn't check these settings just now/);
    expect(stale).toHaveClass("notice-warn");
  });

  it("stops reporting a draft once the address is saved back to the default", async () => {
    // The row's help tells the operator to clear the box to go back to the hosted default, and
    // clearing it saves the empty string. The route stores that as "unset" and answers with the
    // same default string it was already returning, so `savedWebUrl` keeps the same value and
    // the re-seed effect never fires unless the panel re-seeds from the server's response after
    // a save. Without that re-seed, the box would sit empty against a default it now matches:
    // the Save button never goes away, and the panel would keep asking the operator to confirm
    // leaving over a value that is already stored.
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
    // This panel re-seeds on every change of the stored value, where `GeneralPanel` seeds once,
    // so the guard has to track which value the boxes were seeded from, not merely whether they
    // have been seeded. A save, or another tab changing the address, reopens the same risk
    // window a second time, and a plain "have we seeded" flag would miss that second window
    // entirely.
    //
    // The web address is the field this test can reach: the fixture stores a non-empty address,
    // so the box starts empty against it. The check is deterministic because the report runs as
    // an effect, so the spy records the call whether or not the frame is painted.
    const dirty = vi.fn();
    renderPanel([], dirty);

    await waitFor(() =>
      expect(screen.getByPlaceholderText("https://app.plex.tv")).toHaveValue("https://app.plex.tv"),
    );

    expect(dirty).not.toHaveBeenCalledWith(true);
  });

  it("never reports a draft when the stored status is already cached", async () => {
    // This is the warm case of the test above, and the one this guard is actually for. A fresh
    // `QueryClient` mounts this panel cold: `data` is undefined on the first render, so the box
    // and the stored value both start as "" and no comparison can go wrong yet. Every render
    // after the first section switch is warm instead: `["plex"]` is read only by this panel,
    // nothing evicts it within the default window, and `Settings` unmounts and remounts the
    // panel on every section change. So `data` is already there on the first render while the
    // box is still "". A guard seeded from `savedWebUrl` would agree with it on that frame and
    // pass wrongly, and the confirm built on that report would then ask about Plex settings
    // nobody typed. Cold and warm differ by one line, and only the warm case can fail.
    const dirty = vi.fn();
    renderPanel([], dirty, status());

    await waitFor(() =>
      expect(screen.getByPlaceholderText("https://app.plex.tv")).toHaveValue("https://app.plex.tv"),
    );

    expect(dirty).not.toHaveBeenCalledWith(true);
  });

  it("never reports a draft for a cached certificate choice either", async () => {
    // The certificate switch shares the same guard as the address box, so it gets the same warm
    // mount. It does not get a claim of live exposure: today the status read omits the
    // certificate flag while unlinked, and the schema default is `true`, so the stored value and
    // this box's initial value agree on every real server and no operator can reach the state
    // this test drives. The state below is one the API does not currently produce; this comment
    // says so rather than letting the test read as a reproduction of a real scenario. What it
    // pins is the shape of the guard: the flag starts as a sentinel value no stored value can
    // equal, so nobody can rewrite it to seed from the stored value and find out later, once the
    // route starts sending the flag, that the guard had gone inert.
    //
    // The address is stored empty here so the box and its saved value agree, leaving the
    // certificate switch as the only control that can report a draft at all.
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
    // The row is filled by parsing the stored address, so the parsed text and the stored text
    // are not identical, and comparing them directly would call an untouched row an edit. This
    // is an address the operator can save through this very box: `URL.hostname` lowercases the
    // host on the way in, so it comes back spelled differently than it went out. A stored
    // scheme-default port behaves the same way, since `URL.port` is empty for one.
    const user = userEvent.setup();
    const dirty = vi.fn();
    apiMock.plexStatus.mockResolvedValue(
      status({ connection_uri: "https://Plex.Example.net:32400" }),
    );
    renderPanel([], dirty);

    // Settle the mount before watching anything, so what follows is only what OPENING the row
    // reports. Waiting only for a `false` call would be satisfied too early, by the loading
    // state's own report. A settled mount is still the right starting point for a test about
    // opening the row.
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
    // The other half of the guard: deleting `manualDirty` from the report must fail this test.
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
    // The switch only writes once a server is linked (`if (linked)` in its onChange), so before
    // that the choice is a draft that lives only in component state, alongside the link poll.
    // Leaving the section without this report would drop that choice silently, and the next
    // sign-in would probe a self-signed server with checking back on, and fail with nothing on
    // screen explaining why.
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
    // If the save also sent the saved web address, read from `["plex"]`'s cached row, the route
    // would write every field it received. A cached row that has gone stale, through a failed
    // refetch this panel deliberately renders through, or another tab editing the address, would
    // then revert the operator's address without a word, and every "open in Plex" link in the
    // app would point at plex.tv. `web_url` must be absent from the payload, so this checks the
    // exact payload sent rather than the switch's own value.
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

// The three groups below the connection form, driven through a failed refetch.
//
// An undivided `isError` would trade the whole library grid, and separately both Leaving Soon
// switches, for one error paragraph, even while React Query still holds the last good answer.
// So each group renders a stale notice instead, keeping the last good data on screen. Every
// trigger here is a success path: `invalidateAllPlex` fires on all three paths that change which
// server is linked (a switch, a link, and an unlink), and returning to the section past
// `staleTime` refetches on its own. The callers that trigger `invalidateAllPlex` are pinned by
// name in "changing which server is linked" below. Each group is pinned in both directions,
// because a fix that only deleted the `isError` arm would leave a genuinely never-loaded group
// claiming to be merely stale instead of never loaded.
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
  // The sentence this panel uses once several of its reads have failed at once.
  const STALE_PANEL = /Couldn't check the Plex settings just now/;
  const STALE_ANY = /Couldn't check .* just now/;
  // The noun in the stale-read sentence is the `what` prop of StaleReadNotice, which owns the
  // sentence. Deleting either `what` prop here would leave the loose match below still passing,
  // so each read is asserted by its own noun.
  const WHAT_HINT =
    "The stale line's noun is the `what` prop of StaleReadNotice.tsx, which owns the sentence. " +
    'Sibling call sites: PlexPanel\'s own status read (the default, "these settings"), the ' +
    'library grid ("the library list"), the watch history record, the Leaving Soon group ("the ' +
    'Leaving Soon settings"); ' +
    "AboutPanel.tsx, JobsPanel.tsx (the panel and LeavingSoonRow), NotificationsPanel.tsx and " +
    "ServicesPanel.tsx.";

  it("keeps the library grid and its switches when the refetch fails", async () => {
    const queryClient = renderWithClient();
    // Find the cheap text first, then the switch synchronously; this mirrors the Leaving Soon
    // row in SettingsStaleRead.test.tsx. This grid is two reads deep: the panel returns early
    // while `plex` is pending, and the grid then waits on `plex-libraries` behind it. A
    // `findByRole` with a name matcher recomputes accessible names across the whole panel on
    // every 50ms poll, which competes with this test for the same timeout budget in
    // `src/test/setup.ts`.
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
    // The state `invalidateAllPlex` produces: switching to an unreachable Plex server refetches
    // all four of these reads at once. Without this grouped notice, each would show its own
    // amber paragraph, so the operator would read the same failure four times down one panel,
    // and since the notice carries role="alert", hear it announced four times too.
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
    // The line still sits above everything it covers, since it says what is below it may be out
    // of date, and `.panel` uses plain block flow.
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
    // Same shape as the grid above, on the same query key. "Update while read-only" is the
    // second row's label, rendered only once `leaving-soon-settings` has landed, so waiting for
    // it settles the read this switch depends on without walking the tree for names.
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

// The watch-record reset. A title whose measured plays fall to zero reads as unreadable. That
// is correct unless the cause is a rebuilt library, in which case it is every watched title at
// once and nothing is reapable. This reset is the way out, so it has to be reachable, has to say
// what it will do, and must not be reachable by one stray click.
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
   *  The Confirm button is disabled until the box has something in it, and user-event reports a
   *  click on a disabled button as success, so a test that clicks it before typing would send
   *  nothing and then fail later on a state its no-op never produced. */
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
    // The password must reach the server: the route refuses without it, so a call that dropped
    // it would still pass here while returning 403 in the real app.
    // This reads the first argument only, rather than matching the whole call, because React
    // Query hands a mutationFn its own context as a second argument that the client ignores and
    // this assertion has no reason to pin.
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

    // Reopening offers an empty box. If Cancel only closed the form, the admin password would
    // stay in component state for as long as this panel is mounted, and refill the field the
    // next time it opens.
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

    // The second sentence matters: the stored candidates are frozen snapshot data, so the queue
    // does not move until the next scan. Without it, the operator would read the unchanged queue
    // as "that did not work" and reach for the policy next, which does cost files.
    //
    // This checks the visible row only, because the sentence appears on screen twice on
    // purpose: the live region announces the same words, and a query with no scope would find
    // both and throw.
    const status = await screen.findByText(/Forgotten for 1,284 titles/, {
      selector: ".set-status",
    });
    expect(status).toHaveTextContent("The next scan uses what Plex holds now.");
  });

  // The fallback for a failure that arrives with nothing to say. Without it, the operator would
  // see an empty red box. "Nothing changed" is the essential half of that sentence, since
  // someone reading a bare failure cannot tell whether pressing again is safe.
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
  // server's own sentence is what gets rendered. A fixed "couldn't forget the record" message
  // would tell someone locked out to keep pressing.
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
    // be resent. `RestoreCard` does the same thing with a rejected confirm.
    expect(screen.getByLabelText("Admin password")).toHaveValue("");
  });

  // The three states that are not "a password exists" all fail closed: this control withdraws a
  // protection from every title at once, so there is no direction here that is safe to offer on a
  // guess. `DeletionToggle` keeps its OFF direction live on an unreadable safety read, since that
  // direction can only make Reaper safer; this row has no equivalent safe direction.
  // Both exits unmount the form and take the pressed button with them, so focus falls to `<body>`
  // and the next Tab would restart at the top of the page. This test moves focus to the button
  // that reopened the form, which is back in that slot by the next commit. The two Save buttons
  // on this panel make the same handoff, through the same hook.
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

    // The status line is the only thing that moves, and it sits in an unfocused subtree. It
    // reads the same words as the visible sentence.
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

    // This names the reason instead of leaving a blank: a control that simply vanished would
    // read as "this install doesn't have that feature" rather than "Reaper couldn't check."
    await screen.findByText(/couldn't check whether an admin password is set/);
    expect(screen.queryByRole("button", { name: "Forget…" })).toBeNull();
    expect(apiMock.resetWatchEvidence).not.toHaveBeenCalled();
  });

  // The status line answers "do I need to press this at all." This is a table because of the
  // null reading: a scan that never counted plays is different from a scan that counted zero
  // unreadable ones, and the sentence must not claim the last scan found nothing unreadable when
  // it never checked at all.
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

  // "Held back" is this app's phrase for an item with no readable size, used in the planner and
  // on four docs pages, where the fix is a policy allowance. Reusing that phrase for unreadable
  // plays would send the operator to that allowance instead of to Tautulli.
  it("does not describe an unreadable-history item with the unknown-size phrase", async () => {
    apiMock.watchEvidence.mockResolvedValue({ titles: 1284, held_back: 3 });
    renderPanel();
    const status = await screen.findByText(/couldn't read the plays for 3 items/);
    expect(status).not.toHaveTextContent("held back");
    // This also is not a verdict claim: the number shown is not computed from one.
    expect(status).not.toHaveTextContent("kept");
  });

  it("warns that a watched title will score as unwatched until Tautulli is repaired", async () => {
    renderPanel();
    // This states the real cost of pressing the button. A reset that reads as free is one an
    // operator would press without repairing the source, after which every re-added title
    // becomes condemnable on false zero-play counts.
    const warning = await screen.findByText(/scores as never watched/);
    expect(warning).toHaveClass("notice-warn");
    expect(warning).toHaveTextContent("repair its history in Tautulli");
  });
});

describe("saving the Plex web address", () => {
  // The row's inline Save button exists only while the box is dirty, so pressing it removes the
  // pressed control, and the button is disabled while the write is in flight, so focus is
  // already gone by the time it succeeds. Disappearing is not something a screen reader can
  // hear, so this row also announces success through the same live region the Manual address
  // row beside it uses ("Connection saved."), and this test checks that both rows say something
  // out loud.
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

describe("naming the shelf", () => {
  /** The saved answer, so the row re-seeds from the server response rather than from an effect. */
  function savesAs(name: string) {
    apiMock.setLeavingSoonSettings.mockResolvedValue({
      enabled: false,
      allow_unarmed: false,
      name,
      applied_name: "Leaving Soon",
      last: null,
      last_skip: null,
    });
  }

  it("sends the typed name, and reports it as a draft until it is saved", async () => {
    const user = userEvent.setup();
    const dirty = vi.fn();
    savesAs("Last chance");
    renderPanel([discovered(LOCAL)], dirty);

    const box = await screen.findByLabelText("Shelf name");
    expect(box).toHaveValue("Leaving Soon");
    // No Save at rest: the button exists only while the row holds something to save.
    expect(within(box.parentElement as HTMLElement).queryByRole("button")).toBeNull();

    await fill(user, box, "Last chance");
    await waitFor(() => expect(dirty).toHaveBeenLastCalledWith(true));

    await user.click(
      within(box.parentElement as HTMLElement).getByRole("button", { name: "Save" }),
    );
    expect(apiMock.setLeavingSoonSettings.mock.calls[0]?.[0]).toEqual({ name: "Last chance" });
    await waitFor(() => expect(dirty).toHaveBeenLastCalledWith(false));
  });

  it("stops reporting a draft once an emptied box is saved back to the default", async () => {
    // Clearing the box is how the help tells the operator to go back to the default, and
    // clearing it saves the empty string. The route stores that as unset and answers with the
    // same default name it was already returning, so `savedShelfName` keeps the same value and
    // the re-seed effect would never fire without this test's guard. Without it, the box would
    // sit empty against a name it now matches: the Save button never goes away, and the panel
    // would keep asking to confirm leaving over a value that is already stored. The web address
    // row above needs the same fix.
    const user = userEvent.setup();
    const dirty = vi.fn();
    savesAs("Leaving Soon");
    renderPanel([discovered(LOCAL)], dirty);

    const box = await screen.findByLabelText("Shelf name");
    await user.clear(box);
    await waitFor(() => expect(dirty).toHaveBeenLastCalledWith(true));

    await user.click(
      within(box.parentElement as HTMLElement).getByRole("button", { name: "Save" }),
    );
    expect(apiMock.setLeavingSoonSettings.mock.calls[0]?.[0]).toEqual({ name: "" });
    await waitFor(() => expect(box).toHaveValue("Leaving Soon"));
    await waitFor(() => expect(dirty).toHaveBeenLastCalledWith(false));
    expect(within(box.parentElement as HTMLElement).queryByRole("button")).toBeNull();
  });

  it("names the shelf in the switch beside it, from the STORED name", async () => {
    // The switch says what it does to a shelf that exists. Until Save lands, the name in the
    // box is not what the switch would do anything to.
    const user = userEvent.setup();
    savesAs("Last chance");
    renderPanel();

    const box = await screen.findByLabelText("Shelf name");
    await fill(user, box, "Last chance");

    expect(screen.getByRole("switch", { name: 'Show "Leaving Soon" in Plex' })).toBeInTheDocument();
  });
});

describe("the shelf status line", () => {
  // The one sentence on this screen that says how the Leaving Soon shelf is doing. It has to
  // stay in sync with the other status lines that report similar information, and it has to say
  // when a later scan skipped the shelf rather than reporting a stale pass as current.
  const PASS = {
    at: "2026-08-03T02:00:00+00:00",
    movies: 280,
    seasons: 311,
    applied: true,
    ok: true,
    result_reason: { k: "shelf_updated", p: { added: 4, removed: 1 } },
  };
  /** Dated after `PASS`, since the whole decision is which record is newer. `result_reason` is
   *  a real catalog code, but this panel's own status line never reads it: it shows only a
   *  generic "later scan skipped" sentence, while the Jobs row names the reason. So the exact
   *  code does not matter to these assertions, it just has to be a real one, since `shelf()`'s
   *  loose `Record<string, unknown>` typing would not catch a stale shape. */
  const SKIP = {
    at: "2026-08-04T20:06:00+00:00",
    result_reason: { k: "error.leaving_soon.skip_unreachable", p: {} },
  };

  async function statusLine(over: Record<string, unknown>): Promise<string> {
    apiMock.leavingSoonSettings.mockResolvedValue({
      enabled: true,
      allow_unarmed: false,
      name: "Leaving Soon",
      applied_name: "Leaving Soon",
      last: PASS,
      last_skip: null,
      ...over,
    });
    const { container } = renderPanel();
    const line = await waitFor(() => {
      const found = [...container.querySelectorAll(".set-status")].find((n) =>
        n.textContent?.includes("on the shelves"),
      );
      expect(found, "the shelf status line is not on the panel").toBeDefined();
      return found as HTMLElement;
    });
    return line.textContent ?? "";
  }

  /** The whole line except the elapsed-time phrase, which reads the wall clock and is not a
   *  claim this panel makes. Anchored at both ends, so an unexpected lead-in fails the match. */
  const LINE = (lead: string) =>
    new RegExp(
      `^${lead}Last updated .+, 280 movies and 311 seasons on the shelves, ` +
        "next update after the next scan\\.$",
    );

  it("leads with the pass's own sentence, never one worded here", async () => {
    expect(await statusLine({})).toMatch(LINE("4 added, 1 cleared\\. "));
  });

  it("says nothing at all while the shelf is switched off", async () => {
    // If shown here, the line would promise "next update after the next scan" for a scan coded
    // to skip the shelf, and would name counts that a switch-off cleanup had already cleared
    // from Plex. The switch is asserted alongside the missing line, because a whole group
    // disappearing would look the same as a fixed line disappearing to a query for the text
    // alone.
    apiMock.leavingSoonSettings.mockResolvedValue({
      enabled: false,
      allow_unarmed: false,
      name: "Leaving Soon",
      applied_name: "Leaving Soon",
      last: PASS,
      last_skip: null,
    });
    const { container } = renderPanel();

    const shelf = await screen.findByRole("switch", { name: 'Show "Leaving Soon" in Plex' });
    expect(shelf).not.toBeChecked();
    // The watch-history record has its own `.set-status` row on this panel, so this predicate
    // uses the same one `statusLine` does: the shelf line is the one naming the shelves.
    const shelfLine = [...container.querySelectorAll(".set-status")].find((n) =>
      n.textContent?.includes("on the shelves"),
    );
    expect(shelfLine).toBeUndefined();
    expect(screen.queryByText(/next update after the next scan/)).toBeNull();
  });

  it("says a later scan skipped the shelves, and past-tenses the counts it left behind", async () => {
    // Without this line, the panel would report only the completed pass: a confident verdict
    // about a shelf that has stopped updating, on the exact screen an operator visits when they
    // suspect that.
    const line = await statusLine({ last_skip: SKIP });

    expect(line).toContain("A later scan didn't update the shelves. The Jobs page says why.");
    expect(line).toContain("were on the shelves at the last update");
    expect(line).not.toContain("4 added, 1 cleared");
  });

  it("goes back to the pass once a later one lands", async () => {
    // This direction proves the panel compares the two dates. Nothing clears a skip on its own,
    // so without this comparison the panel would report a recovered shelf as broken forever.
    const line = await statusLine({
      last: { ...PASS, at: "2026-08-04T22:30:00+00:00" },
      last_skip: SKIP,
    });

    expect(line).toContain("4 added, 1 cleared.");
    expect(line).not.toContain("didn't update the shelves");
  });

  it("opens with the shelf Plex still shows, when a rename has not been carried across", async () => {
    // The one fact on this screen that contradicts what the operator is looking at: the box two
    // rows up shows one name, but the library still shows another, and the counts that follow
    // are about the shelf under the old name. That is why this sentence leads.
    const line = await statusLine({ name: "Last chance", applied_name: "Leaving Soon" });

    expect(line).toMatch(
      /^Plex still shows "Leaving Soon"\. The next update renames it\. 4 added, 1 cleared\./,
    );
  });

  it("stops saying it once a pass has carried the rename across", async () => {
    const line = await statusLine({ name: "Last chance", applied_name: "Last chance" });

    expect(line).not.toContain("Plex still shows");
  });

  it("opens with the date, not a stray period, for a row stored before summaries existed", async () => {
    // `ok` and a result reason were added to the stored row after the shelf shipped, and the
    // stored JSON is never migrated, so an old row can be read back with a legacy reason that
    // has empty text.
    expect(
      await statusLine({ last: { ...PASS, result_reason: { k: "legacy", p: { text: "" } } } }),
    ).toMatch(LINE(""));
  });

  it("groups every number on the line the one way, whatever the browser's locale", async () => {
    // The lead sentence comes from the server, comma-grouped by Python's `:,` formatting, and
    // the server cannot know the browser's locale. `count` follows the browser's locale
    // instead, so in a de-DE browser the two together would read "1,234 added, 5,678 cleared.
    // Last updated …, 1.234 movies," two different thousands separators in one sentence,
    // neither wrong alone.
    //
    // This test drives a non-English locale so it can fail if the code reverts to `count` for
    // the server's own numbers: under en-US both formatters agree and the assertion could not
    // tell them apart. The locale is stubbed on `Number.prototype.toLocaleString`, which is the
    // call both formatters actually make. Spying on `Intl.NumberFormat` instead would not reach
    // it.
    const original = Number.prototype.toLocaleString;
    vi.spyOn(Number.prototype, "toLocaleString").mockImplementation(function (
      this: number,
      locales?: Intl.LocalesArgument,
      options?: Intl.NumberFormatOptions,
    ) {
      return original.call(this, locales ?? "de-DE", options);
    });
    try {
      const line = await statusLine({
        last: {
          ...PASS,
          movies: 1234,
          seasons: 5678,
          result_reason: { k: "shelf_updated", p: { added: 1234, removed: 5678 } },
        },
      });

      expect(line).toContain("1,234 added, 5,678 cleared");
      expect(line).toContain("1,234 movies and 5,678 seasons");
      expect(line).not.toContain("1.234");
      expect(line).not.toContain("5.678");
    } finally {
      vi.mocked(Number.prototype.toLocaleString).mockRestore();
    }
  });
});
