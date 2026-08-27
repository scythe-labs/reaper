// SPDX-License-Identifier: AGPL-3.0-or-later
// The service edit modal has two maps: the HD/4K library map (Sonarr/Radarr) and the multi-Seerr
// service-to-instance map (Seerr). Both follow the same pattern, pinned here: each row pairs with
// a select, a suggested but unconfirmed pick wears a "suggested" tag that clears once the operator
// chooses, saving sends the map shown on screen, and a list that fails to load shows a notice
// instead of an empty list.
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Instance, PlexLibrary, RootFolder, SeerrService } from "../api";
import { expectNoA11yViolations } from "../test/a11y";
import { fill } from "../test/forms";
import { testQueryClient } from "../test/queryClient";
import { renderWithProviders } from "../test/renderWithProviders";
import { Announcer } from "../announce";
import { ServiceModal } from "./ServiceModal";

const { apiMock } = await vi.hoisted(async () => ({
  apiMock: (await import("../test/apiMock")).makeApiMock(),
}));

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  api: apiMock,
}));

// The mocks are shared across tests, so this clears their call history first. A later test
// reading `updateInstance.mock.calls[0]` would otherwise see an earlier test's save, not its own.
beforeEach(() => {
  vi.clearAllMocks();
});

function sonarr(overrides: Partial<Instance> = {}): Instance {
  return {
    id: 3,
    kind: "sonarr",
    name: "Main",
    base_url: "http://10.0.0.5:8989",
    external_url: null,
    enabled: true,
    verify_tls: true,
    add_import_exclusion: false,
    plex_library_map: {},
    service_instance_map: {},
    has_key: true,
    detected_version: null,
    last_ok_at: null,
    last_error: null,
    ...overrides,
  };
}

function seerr(overrides: Partial<Instance> = {}): Instance {
  return {
    ...sonarr(),
    id: 5,
    kind: "seerr",
    name: "Primary",
    base_url: "http://10.0.0.9:5055",
    ...overrides,
  };
}

const LIBRARIES: PlexLibrary[] = [
  { key: 1, title: "TV", kind: "show", enabled: true },
  { key: 2, title: "TV 4K", kind: "show", enabled: true },
  { key: 3, title: "Movies", kind: "movie", enabled: true },
];

function renderModal(
  instance: Instance,
  folders: RootFolder[] | Error,
  libraries: PlexLibrary[] | Error = LIBRARIES,
) {
  if (folders instanceof Error) apiMock.instanceRootFolders.mockRejectedValue(folders);
  else apiMock.instanceRootFolders.mockResolvedValue(folders);
  if (libraries instanceof Error) apiMock.plexLibraries.mockRejectedValue(libraries);
  else apiMock.plexLibraries.mockResolvedValue(libraries);
  // A re-sync returns the same list the read did, so an empty list stays empty. This lets the
  // "nothing to map to" case run instead of the hook's own sync quietly filling it in.
  apiMock.syncPlexLibraries.mockResolvedValue(libraries instanceof Error ? [] : libraries);
  apiMock.updateInstance.mockResolvedValue(instance);
  const onClose = vi.fn();
  renderWithProviders(<ServiceModal kind="sonarr" instance={instance} onClose={onClose} />);
  return { onClose };
}

/** The library select for one root-folder row, found by that row's folder label. */
function selectForFolder(path: string): HTMLSelectElement {
  // Looked up by accessible name, not by DOM position. A screen reader also finds a control by
  // its accessible name, so a test that reached through the DOM instead could pass while every
  // select is still unnamed.
  return screen.getByLabelText(`Plex library for ${path}`) as HTMLSelectElement;
}

describe("ServiceModal HD/4K library map", () => {
  // Each root folder is paired with a Plex library by its own select, and pairing one wrong
  // points Reaper's watch history at the wrong library. Every select has to say which folder it
  // belongs to, or the operator picks in the dark.
  it("has no accessibility violations", async () => {
    renderModal(sonarr(), [
      { path: "/tv", suggested_library: "TV" },
      { path: "/tv-4k", suggested_library: "TV 4K" },
    ]);
    await waitFor(() => expect(selectForFolder("/tv-4k").value).toBe("TV 4K"));
    await expectNoA11yViolations();
  });

  it("prefills a folder with its suggested library, tagged 'suggested'", async () => {
    renderModal(sonarr(), [
      { path: "/tv", suggested_library: "TV" },
      { path: "/tv-4k", suggested_library: "TV 4K" },
    ]);
    // The select for /tv-4k lands on its suggestion, and the row shows the tag.
    await waitFor(() => expect(selectForFolder("/tv-4k").value).toBe("TV 4K"));
    expect(selectForFolder("/tv").value).toBe("TV");
    expect(screen.getAllByText("suggested")).toHaveLength(2);
  });

  it("states the chosen library as text, so two long names cannot read alike", async () => {
    // A native <select> clips its selected option to the control's width and cannot wrap, so two
    // libraries differing only past the visible prefix render as one string in the box. The
    // plain text restatement below the select is what actually shows the whole name, so this
    // asserts it carries the full string rather than a still-truncated copy.
    const shared = "Movies Archive Second Floor Overflow";
    renderModal(
      sonarr(),
      [
        { path: "/tv-4k", suggested_library: `${shared} 4K` },
        { path: "/tv-hd", suggested_library: `${shared} HD` },
      ],
      [
        { key: 1, title: `${shared} 4K`, kind: "show", enabled: true },
        { key: 2, title: `${shared} HD`, kind: "show", enabled: true },
      ],
    );
    await waitFor(() => expect(selectForFolder("/tv-4k").value).toBe(`${shared} 4K`));

    const echoed = [...document.querySelectorAll(".pl-echo")].map((e) => e.textContent ?? "");
    expect(echoed).toEqual([`${shared} 4K`, `${shared} HD`]);
    // This is the property the operator actually depends on. No two rows read alike.
    expect(new Set(echoed).size).toBe(echoed.length);
  });

  it("says nothing under a folder still on 'Not set'", async () => {
    // The restatement exists to show a value that would otherwise clip, and "Not set" already
    // reads fine at any width. Rendering a copy of it anyway would add nothing to every unmapped
    // row.
    renderModal(sonarr(), [{ path: "/tv", suggested_library: null }]);
    await waitFor(() => expect(selectForFolder("/tv").value).toBe(""));
    expect(document.querySelectorAll(".pl-echo")).toHaveLength(0);
  });

  it("clears the 'suggested' tag once the operator picks a value", async () => {
    renderModal(sonarr(), [{ path: "/tv-4k", suggested_library: "TV 4K" }]);
    await waitFor(() => expect(selectForFolder("/tv-4k").value).toBe("TV 4K"));
    expect(screen.getByText("suggested")).toBeInTheDocument();
    await userEvent.selectOptions(selectForFolder("/tv-4k"), "TV");
    expect(screen.queryByText("suggested")).not.toBeInTheDocument();
  });

  it("sends the map the operator sees on save, dropping 'Not set' folders", async () => {
    renderModal(sonarr(), [
      { path: "/tv", suggested_library: "TV" },
      { path: "/tv-oddball", suggested_library: null },
    ]);
    await waitFor(() => expect(selectForFolder("/tv").value).toBe("TV"));
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(apiMock.updateInstance).toHaveBeenCalled());
    const body = apiMock.updateInstance.mock.calls[0]![1] as {
      plex_library_map?: Record<string, string>;
    };
    // The suggested folder is included. The unset one is dropped rather than stored as "".
    expect(body.plex_library_map).toEqual({ "/tv": "TV" });
  });

  it("keeps a saved mapping and does not tag it 'suggested'", async () => {
    // The suggestion differs from what is saved, so this proves the stored pick wins because it
    // was stored, not because both values happened to agree. The second folder proves the
    // prefill effect actually ran: without it, the assertion could pass on the first render,
    // before the effect has a chance to overwrite anything, and a prefill that clobbers a saved
    // pick would still read as passing.
    renderModal(sonarr({ plex_library_map: { "/tv": "TV" } }), [
      { path: "/tv", suggested_library: "TV 4K" },
      { path: "/tv-spare", suggested_library: "TV 4K" },
    ]);
    await waitFor(() => expect(selectForFolder("/tv-spare").value).toBe("TV 4K"));

    expect(selectForFolder("/tv").value).toBe("TV");
    // A folder already saved is not a suggestion, so only the unsaved row is tagged.
    expect(screen.getAllByText("suggested")).toHaveLength(1);
  });

  // Saving rebuilds the map from the folders currently in hand, so an entry for a folder the *arr
  // no longer has gets dropped. That is correct while the list is current, and destructive when
  // the list is only stale: a failed refetch keeps the last good list on screen with a
  // stale-data notice over it, so a test that only checks "is there data" cannot tell a fresh
  // list from an old one. The map is what tells an HD copy from a 4K one apart, so both
  // directions are tested here. A fix that stopped pruning altogether would leave a folder that
  // really is gone mapped forever.
  describe("pruning the stored map", () => {
    /** Renders the modal with a stored map wider than the folder list it gets back, and returns
     *  the query client so the test can force a failed refetch behind the grid. */
    function renderWithStaleableFolders() {
      const instance = sonarr({ plex_library_map: { "/tv": "TV", "/archive": "TV 4K" } });
      apiMock.instanceRootFolders.mockResolvedValue([{ path: "/tv", suggested_library: "TV" }]);
      apiMock.plexLibraries.mockResolvedValue(LIBRARIES);
      apiMock.updateInstance.mockResolvedValue(instance);
      const queryClient = testQueryClient();
      renderWithProviders(<ServiceModal kind="sonarr" instance={instance} onClose={vi.fn()} />, {
        client: queryClient,
      });
      return queryClient;
    }

    async function savedMap(): Promise<Record<string, string> | undefined> {
      await userEvent.click(screen.getByRole("button", { name: "Save" }));
      await waitFor(() => expect(apiMock.updateInstance).toHaveBeenCalled());
      const body = apiMock.updateInstance.mock.calls[0]![1] as {
        plex_library_map?: Record<string, string>;
      };
      return body.plex_library_map;
    }

    it("drops a folder the instance no longer has, on a list it just read", async () => {
      renderWithStaleableFolders();
      await waitFor(() => expect(selectForFolder("/tv").value).toBe("TV"));

      // The read succeeded, so the missing "/archive" folder is a confirmed answer, not a gap.
      expect(await savedMap()).toEqual({ "/tv": "TV" });
    });

    it("keeps it when the folder list is only out of date", async () => {
      const queryClient = renderWithStaleableFolders();
      await waitFor(() => expect(selectForFolder("/tv").value).toBe("TV"));

      apiMock.instanceRootFolders.mockRejectedValue(new Error("unreachable"));
      await act(async () => {
        await queryClient.invalidateQueries({ queryKey: ["instance-root-folders"] });
      });
      // The stale-data notice is up and the grid is still visible. This is exactly the state the
      // guard is meant to handle.
      expect(
        await screen.findByText(/couldn't check this instance's folders/i),
      ).toBeInTheDocument();
      expect(selectForFolder("/tv")).toBeInTheDocument();

      // Nothing confirmed /archive is gone, so the save does not delete it.
      expect(await savedMap()).toEqual({ "/tv": "TV", "/archive": "TV 4K" });
    });
  });

  it("shows a notice, not an empty list, when the folders can't be read", async () => {
    renderModal(sonarr(), new Error("unreachable"));
    expect(await screen.findByText(/couldn't read this instance's folders/i)).toBeInTheDocument();
  });

  it("does not claim the operator has no libraries when the list can't be read", async () => {
    // A failed fetch empties the options the same way a genuinely empty library list does. Left
    // unhandled, the "none yet" sentence would then state as fact something Reaper never
    // actually learned, and send the operator to re-sync a list that is already there.
    renderModal(sonarr(), [{ path: "/tv", suggested_library: "TV" }], new Error("unreachable"));
    expect(await screen.findByText(/couldn't read your Plex libraries/i)).toBeInTheDocument();
    expect(screen.queryByText(/No Plex libraries yet/i)).not.toBeInTheDocument();
  });

  it("keeps the 'none yet' sentence for a list that really is empty", async () => {
    renderModal(sonarr(), [{ path: "/tv", suggested_library: null }], []);
    // The hook runs the sync itself now, so the panel no longer sends anyone to Plex settings
    // to press Sync. The only honest thing left to say is that the server has no library of
    // this kind.
    expect(await screen.findByText(/No TV libraries in Plex yet/i)).toBeInTheDocument();
    expect(screen.queryByText(/Sync them in Plex settings/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/couldn't read your Plex libraries/i)).not.toBeInTheDocument();
  });

  it("syncs a library list that has never been synced, instead of offering nothing", async () => {
    // `GET /plex/libraries` answers "as last synced." Before the wizard ran a sync, every
    // picker here offered nothing, even though the "suggested" tags beside them come from a
    // live Plex read and named libraries that plainly existed. An empty read now triggers
    // exactly one sync.
    apiMock.instanceRootFolders.mockResolvedValue([{ path: "/tv", suggested_library: "TV" }]);
    apiMock.plexLibraries.mockResolvedValue([]);
    apiMock.syncPlexLibraries.mockResolvedValue(LIBRARIES);
    apiMock.updateInstance.mockResolvedValue(sonarr());
    renderWithProviders(<ServiceModal kind="sonarr" instance={sonarr()} onClose={vi.fn()} />);
    await waitFor(() => expect(apiMock.syncPlexLibraries).toHaveBeenCalledTimes(1));
    // And the pickers it fills really do offer the synced libraries.
    await waitFor(() =>
      expect(within(selectForFolder("/tv")).getByRole("option", { name: "TV 4K" })).toBeDefined(),
    );
  });

  it("does not sync a list that merely failed to load", async () => {
    // A read failure is not an empty list. Answering a failure with a write would hide the
    // state the operator needs to see. `libraries.data` is undefined here, never `[]`.
    renderModal(sonarr(), [{ path: "/tv", suggested_library: null }], new Error("plex down"));
    expect(await screen.findByText(/couldn't read your Plex libraries/i)).toBeInTheDocument();
    expect(apiMock.syncPlexLibraries).not.toHaveBeenCalled();
  });

  it("names every picker after its own folder", async () => {
    // The folder name sits in a sibling cell the select isn't labeled by, so without a label
    // every row would announce as "combobox, Not set" and a screen reader user couldn't tell
    // which folder they were mapping. Picking the wrong one points Leaving Soon writes and the
    // Never-Reap read at a library Reaper was never meant to touch.
    renderModal(sonarr(), [
      { path: "/tv", suggested_library: "TV" },
      { path: "/tv-4k", suggested_library: "TV 4K" },
    ]);
    await waitFor(() => expect(selectForFolder("/tv-4k").value).toBe("TV 4K"));
    // Every combobox on the panel has a name, and no two rows share one.
    const named = screen.getAllByRole("combobox").map((c) => c.getAttribute("aria-label"));
    expect(named).toEqual(["Plex library for /tv", "Plex library for /tv-4k"]);
    expect(screen.queryAllByRole("combobox", { name: "" })).toHaveLength(0);
    // The spoken name is derived from the folder cell, and every helper in this file reaches
    // these selects through that name, so nothing else here checks that the cell itself still
    // renders. Removing the folder column would leave every assertion above passing, which is
    // the same kind of blind spot a missing label caused before, one layer over.
    expect(screen.getByText("/tv")).toBeInTheDocument();
    expect(screen.getByText("/tv-4k")).toBeInTheDocument();
  });
});

describe("ServiceModal external URL", () => {
  it("seeds the field from the instance and sends it, trimmed, on save", async () => {
    renderModal(sonarr({ external_url: "https://tv.example.com" }), []);
    const field = screen.getByLabelText("External URL") as HTMLInputElement;
    expect(field.value).toBe("https://tv.example.com");

    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(apiMock.updateInstance).toHaveBeenCalled());
    const body = apiMock.updateInstance.mock.calls[0]![1] as { external_url?: string };
    expect(body.external_url).toBe("https://tv.example.com");
  });

  it("sends a blank string when cleared, so the stored external URL is removed", async () => {
    renderModal(sonarr({ external_url: "https://tv.example.com" }), []);
    await userEvent.clear(screen.getByLabelText("External URL"));

    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(apiMock.updateInstance).toHaveBeenCalled());
    const body = apiMock.updateInstance.mock.calls[0]![1] as { external_url?: string };
    // A blank value clears the stored one back to null, so links fall back to base_url.
    expect(body.external_url).toBe("");
  });

  it("blocks the save and shows an error when the external URL is not a web address", async () => {
    // A scheme-less paste is caught before save, mirroring the server's own 422 rejection, so
    // the value is never sent to be stored as typed.
    renderModal(sonarr({ external_url: null }), []);
    // This uses a session rather than the direct API the rest of the test uses. `fill` takes
    // the `UserEvent` that `setup()` returns, and the module object is a different type.
    await fill(userEvent.setup(), screen.getByLabelText("External URL"), "tv.example.com:8989");

    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText(/must be a full web address/i)).toBeInTheDocument();
    expect(apiMock.updateInstance).not.toHaveBeenCalled();
  });
});

const ARR_INSTANCES: Instance[] = [
  sonarr({ id: 3, kind: "sonarr", name: "Main Sonarr" }),
  sonarr({ id: 4, kind: "sonarr", name: "Restricted Sonarr" }),
  sonarr({ id: 7, kind: "radarr", name: "Main Radarr" }),
];

function renderSeerrModal(
  instance: Instance,
  services: SeerrService[] | Error,
  arrs: Instance[] | Error = ARR_INSTANCES,
) {
  if (services instanceof Error) apiMock.instanceSeerrServices.mockRejectedValue(services);
  else apiMock.instanceSeerrServices.mockResolvedValue(services);
  if (arrs instanceof Error) apiMock.instances.mockRejectedValue(arrs);
  else apiMock.instances.mockResolvedValue(arrs);
  apiMock.updateInstance.mockResolvedValue(instance);
  return renderWithProviders(<ServiceModal kind="seerr" instance={instance} onClose={vi.fn()} />);
}

/** What each service row shows, in document order.
 *
 *  Read off the cell rather than through `getByText`, because the question is what the whole
 *  cell says. A tag the operator needs changes this string, and `getByText` for the bare name
 *  would still pass on a row that renders only the name. */
function visibleServiceRows(container: HTMLElement): string[] {
  return [...container.querySelectorAll(".pl-root")].map((c) => c.textContent ?? "");
}

/** The instance select for one service row, found by that row's service name and media kind. */
function selectForService(name: string, media: "TV" | "Movies" = "TV"): HTMLSelectElement {
  // By accessible name, for the reason given on selectForFolder above. The media kind is part
  // of that name because it is part of the row's identity. Two services can share a name.
  return screen.getByLabelText(`Connection for ${name}, ${media}`) as HTMLSelectElement;
}

describe("ServiceModal multi-Seerr service map", () => {
  it("states the chosen connection as text too, on the same terms as the library picker", async () => {
    // This grid mirrors the folder grid above, so it shares the same clipping problem. This
    // picker stores the instance id while the operator reads its name, so the plain text
    // restatement is looked up from the map rather than read straight off it. A lookup that
    // silently returns nothing would leave this row's restatement bare even after the folder
    // row above was fixed.
    const shared = "Sonarr Second Floor Overflow";
    renderSeerrModal(
      seerr(),
      [
        { service_id: 2, kind: "sonarr", name: "Main TV", is_4k: false, suggested_instance_id: 3 },
        { service_id: 5, kind: "sonarr", name: "Spare TV", is_4k: false, suggested_instance_id: 4 },
      ],
      [
        sonarr({ id: 3, kind: "sonarr", name: `${shared} 4K` }),
        sonarr({ id: 4, kind: "sonarr", name: `${shared} HD` }),
      ],
    );
    await waitFor(() => expect(selectForService("Main TV").value).toBe("3"));

    const echoed = [...document.querySelectorAll(".pl-echo")].map((e) => e.textContent ?? "");
    expect(echoed).toEqual([`${shared} 4K`, `${shared} HD`]);
    expect(new Set(echoed).size).toBe(echoed.length);
  });

  it("prefills a service with its suggested instance, tagged 'suggested'", async () => {
    renderSeerrModal(seerr(), [
      { service_id: 2, kind: "sonarr", name: "Main TV", is_4k: false, suggested_instance_id: 3 },
    ]);
    // The select lands on the suggested instance (id 3), and the row shows the tag.
    await waitFor(() => expect(selectForService("Main TV").value).toBe("3"));
    expect(screen.getByText("suggested")).toBeInTheDocument();
  });

  it("only offers instances of the service's own kind", async () => {
    renderSeerrModal(seerr(), [
      { service_id: 2, kind: "sonarr", name: "Main TV", is_4k: false, suggested_instance_id: null },
    ]);
    await waitFor(() => expect(selectForService("Main TV")).toBeInTheDocument());
    // A Sonarr service lists only the two Sonarr instances, never the Radarr one.
    const options = within(selectForService("Main TV").parentElement as HTMLElement)
      .getAllByRole("option")
      .map((o) => o.textContent);
    expect(options).toEqual(["Not set", "Main Sonarr", "Restricted Sonarr"]);
  });

  it("sends the map the operator sees on save, dropping 'Not set' services", async () => {
    renderSeerrModal(seerr(), [
      { service_id: 2, kind: "sonarr", name: "Main TV", is_4k: false, suggested_instance_id: 3 },
      {
        service_id: 9,
        kind: "sonarr",
        name: "Unmapped",
        is_4k: false,
        suggested_instance_id: null,
      },
    ]);
    await waitFor(() => expect(selectForService("Main TV").value).toBe("3"));
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(apiMock.updateInstance).toHaveBeenCalled());
    const body = apiMock.updateInstance.mock.calls[0]![1] as {
      service_instance_map?: Record<string, number>;
    };
    // The mapped service is sent keyed by "{kind}:{serviceId}". The unset one is dropped.
    expect(body.service_instance_map).toEqual({ "sonarr:2": 3 });
  });

  it("does not collide a Sonarr and a Radarr service that share a serviceId", async () => {
    // Seerr numbers Sonarr and Radarr services separately, so both can have a serviceId of 0.
    // Each row must prefill its own suggestion. The movie row must not read the TV row's value.
    renderSeerrModal(seerr(), [
      { service_id: 0, kind: "sonarr", name: "HD TV", is_4k: false, suggested_instance_id: 3 },
      { service_id: 0, kind: "radarr", name: "HD Movies", is_4k: false, suggested_instance_id: 7 },
    ]);
    await waitFor(() => expect(selectForService("HD TV").value).toBe("3"));
    expect(selectForService("HD Movies", "Movies").value).toBe("7"); // not "Not set", not "3"
    expect(screen.getAllByText("suggested")).toHaveLength(2);
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(apiMock.updateInstance).toHaveBeenCalled());
    const body = apiMock.updateInstance.mock.calls[0]![1] as {
      service_instance_map?: Record<string, number>;
    };
    expect(body.service_instance_map).toEqual({ "sonarr:0": 3, "radarr:0": 7 });
  });

  it("names every picker after its own service, 4K included", async () => {
    // A portal routinely carries an HD and a 4K service under one name, so the tag is the only
    // thing telling those two rows apart. It has to be spoken, not just shown.
    const { container } = renderSeerrModal(seerr(), [
      { service_id: 2, kind: "sonarr", name: "Main TV", is_4k: false, suggested_instance_id: 3 },
      { service_id: 5, kind: "sonarr", name: "Main TV", is_4k: true, suggested_instance_id: null },
    ]);
    await waitFor(() => expect(selectForService("Main TV").value).toBe("3"));
    const named = screen.getAllByRole("combobox").map((c) => c.getAttribute("aria-label"));
    expect(named).toEqual(["Connection for Main TV, TV", "Connection for Main TV 4K, TV"]);
    expect(screen.queryAllByRole("combobox", { name: "" })).toHaveLength(0);
    // As on the folder grid, both helpers reach these selects by their spoken name, so the
    // visible cell they are derived from is otherwise unobserved by this test.
    expect(visibleServiceRows(container)).toEqual(["Main TVTV", "Main TVTV4K"]);
  });

  it("names a TV and a Movies service apart when the portal gives them one name", async () => {
    // The row's identity is kind plus id, so the name has to carry the kind too. Seerr numbers
    // and names its TV and Movies lists independently, so one portal can hold a TV service and
    // a Movies service both called "Media." With the name alone, those two rows would announce
    // identically and the operator could map their movie requests onto a TV connection.
    renderSeerrModal(seerr(), [
      { service_id: 0, kind: "sonarr", name: "Media", is_4k: false, suggested_instance_id: 3 },
      { service_id: 0, kind: "radarr", name: "Media", is_4k: false, suggested_instance_id: 7 },
    ]);
    await waitFor(() => expect(selectForService("Media", "TV").value).toBe("3"));
    expect(selectForService("Media", "Movies").value).toBe("7");
    const named = screen.getAllByRole("combobox").map((c) => c.getAttribute("aria-label"));
    expect(named).toEqual(["Connection for Media, TV", "Connection for Media, Movies"]);
    expect(new Set(named).size).toBe(named.length);
  });

  it("shows the media kind on the row, not only in its spoken name", async () => {
    // The sighted half of the test above. The name was announced with its kind but drawn
    // without one, so a screen reader could tell the portal's TV service from its Movies
    // service while an operator reading the grid could not, and the row they pick decides
    // which connection Reaper deletes a request's copy through. Both tags are pinned together
    // because they are one cell. A fix that drew the kind and dropped 4K would trade one
    // collision for another.
    const { container } = renderSeerrModal(seerr(), [
      { service_id: 0, kind: "sonarr", name: "Media", is_4k: false, suggested_instance_id: 3 },
      { service_id: 0, kind: "radarr", name: "Media", is_4k: false, suggested_instance_id: 7 },
      { service_id: 1, kind: "radarr", name: "Media", is_4k: true, suggested_instance_id: null },
    ]);
    await waitFor(() => expect(selectForService("Media", "TV").value).toBe("3"));
    const rows = visibleServiceRows(container);
    expect(rows).toEqual(["MediaTV", "MediaMovies", "MediaMovies4K"]);
    // This is the point of the tags. Three rows the portal named identically now read apart.
    expect(new Set(rows).size).toBe(rows.length);
  });

  it("says the media kind the same way to both audiences", async () => {
    // The visible tag and the spoken name state one fact, and a drift between them leaves one
    // audience reading a row the other cannot. Driven through both kinds, because a helper
    // hardcoded to one spelling would read correct only for the kind it was written for.
    const { container } = renderSeerrModal(seerr(), [
      { service_id: 0, kind: "sonarr", name: "Media", is_4k: false, suggested_instance_id: 3 },
      { service_id: 0, kind: "radarr", name: "Media", is_4k: false, suggested_instance_id: 7 },
    ]);
    await waitFor(() => expect(selectForService("Media", "TV").value).toBe("3"));
    const tags = [...container.querySelectorAll(".pl-root .pl-tag")].map((t) => t.textContent);
    expect(tags).toEqual(["TV", "Movies"]);
    // Every tag drawn is a word the picker beside it also speaks, so neither can move alone.
    const spoken = screen.getAllByRole("combobox").map((c) => c.getAttribute("aria-label") ?? "");
    tags.forEach((tag, i) => expect(spoken[i]).toBe(`Connection for Media, ${tag}`));
  });

  it("shows a notice, not an empty list, when the services can't be read", async () => {
    renderSeerrModal(seerr(), new Error("forbidden"));
    expect(await screen.findByText(/couldn't read this portal's services/i)).toBeInTheDocument();
  });

  it("does not claim there are no Sonarr or Radarr connections when the list can't be read", async () => {
    // The same trap as above, on the other picker: every `instanceOptions` comes back empty.
    renderSeerrModal(
      seerr(),
      [{ service_id: 2, kind: "sonarr", name: "Main TV", is_4k: false, suggested_instance_id: 3 }],
      new Error("unreachable"),
    );
    expect(
      await screen.findByText(/couldn't read your Sonarr and Radarr connections/i),
    ).toBeInTheDocument();
    expect(screen.queryByText(/No Sonarr or Radarr connections yet/i)).not.toBeInTheDocument();
  });
});

describe("ServiceModal while a save is in flight", () => {
  it("refuses to close, so the failure it is about to show survives", async () => {
    // The scrim, Escape, and the close button must not tear the modal down while a save is in
    // flight. A save can still fail after the modal closes, for example a 409 for a duplicate
    // service name, and a response landing on an unmounted component would never invalidate the
    // caches, leaving the operator believing the change had saved.
    const { onClose } = renderModal(sonarr(), [{ path: "/tv", suggested_library: "TV" }]);
    apiMock.updateInstance.mockReturnValue(new Promise(() => {})); // in flight, forever
    await userEvent.click(await screen.findByRole("button", { name: "Save" }));

    await waitFor(() => expect(screen.getByRole("button", { name: "Saving…" })).toBeDisabled());
    expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
    await userEvent.keyboard("{Escape}");
    expect(onClose).not.toHaveBeenCalled();
  });
});

describe("what a screen reader calls the host box", () => {
  // The scheme renders as a prefix fused inside the field's box, and the <label> wraps both, so
  // without care the box's name would include it: "Hostname or IP http colon slash slash," with
  // a screen reader hearing punctuation instead of the field's actual purpose.
  //
  // This checks `toHaveAccessibleName`, not `getByLabelText`, because the accessible name is
  // what is actually under test here, and only the name computation honors `aria-hidden`. A
  // substring lookup like `getByLabelText(/Hostname or IP/)` would match the noisy name just as
  // happily.
  const hostBox = () => screen.getByLabelText(/Hostname or IP/);

  it("names it for what goes in it, not for the scheme drawn beside it", async () => {
    renderModal(sonarr(), []);
    expect(hostBox()).toHaveAccessibleName("Hostname or IP");
  });

  it("keeps that name with SSL on, where the prefix is a different string", async () => {
    // Both branches of the prefix are driven, since the name must not depend on which one
    // renders. The toggle is the control that states the scheme in words, and it stays
    // reachable.
    renderModal(sonarr({ base_url: "https://10.0.0.5:8989" }), []);
    const toggle = screen.getByRole("switch", { name: /Use SSL/i });
    expect(toggle).toBeChecked();
    expect(hostBox()).toHaveAccessibleName("Hostname or IP");
  });
});

describe("what a screen reader hears when a connection is tested", () => {
  // Pressing "Test connection" renders the result as a badge, and a badge on its own sits in no
  // live region, so a screen reader user would only learn the outcome by navigating onto it.
  // There is no other route to hear a failure either: `instances.py` never raises for a failed
  // test, so an unreachable host arrives as an ordinary 200 with `ok=False` through `onSuccess`,
  // which never reaches the shared error notice. Whether Reaper can reach the Sonarr it deletes
  // through is worth hearing out loud.
  const spoken = () =>
    [...document.querySelectorAll('[aria-live="polite"]')].map((n) => n.textContent).join("");

  /** The ADD form, because "Test connection" only appears there. A saved instance is tested
   *  from its Settings row instead, through `testSavedInstance`, which shares this same
   *  behavior. */
  async function renderWithAnnouncer() {
    apiMock.instanceRootFolders.mockResolvedValue([]);
    apiMock.plexLibraries.mockResolvedValue(LIBRARIES);
    renderWithProviders(
      <>
        <Announcer />
        <ServiceModal kind="sonarr" instance={null} onClose={vi.fn()} />
      </>,
    );
    const user = userEvent.setup();
    await fill(user, screen.getByLabelText(/Hostname or IP/), "10.0.0.5");
    await user.type(screen.getByLabelText(/^API key$/), "k");
    return user;
  }

  it("says a connection was reached, and which version", async () => {
    apiMock.testInstance.mockResolvedValue({
      ok: true,
      detail_reason: { k: "legacy", p: { text: "Connected to Sonarr." } },
      version: "4.0.1",
    });
    const user = await renderWithAnnouncer();

    await user.click(await screen.findByRole("button", { name: /Test connection/i }));

    // "version 4.0.1" is spelled out here, while the badge shows "(v4.0.1)," because a screen
    // reader voices a bare "v" as the letter V. This is the only deliberate difference between
    // the two copies.
    await waitFor(() => expect(spoken()).toBe("Passed: Connected to Sonarr. (version 4.0.1)"));
  });

  it("says a connection FAILED, which arrives as an ordinary 200", async () => {
    apiMock.testInstance.mockResolvedValue({
      ok: false,
      detail_reason: { k: "legacy", p: { text: "Couldn't reach it. Check the address." } },
      version: null,
    });
    const user = await renderWithAnnouncer();

    await user.click(await screen.findByRole("button", { name: /Test connection/i }));

    await waitFor(() => expect(spoken()).toBe("Failed: Couldn't reach it. Check the address."));
    // The same string the badge renders for a reader who does navigate to it.
    expect(document.querySelector(".test-badge")?.textContent).toContain(
      "Failed: ✗ Couldn't reach it. Check the address.",
    );
  });
});

describe("what the connection badge vouches for", () => {
  // The badge must be cleared whenever the hostname or key it was tested against changes. An
  // indicator has to describe the state it was actually computed from, or a "Passed" badge could
  // sit on screen beside credentials nobody has tried.
  const badge = () => document.querySelector(".test-badge");
  const hostBox = () => screen.getByLabelText(/Hostname or IP/);
  const keyBox = () => screen.getByLabelText(/^API key$/);

  /** The ADD form with a passed test already on screen. "Test connection" is only offered while
   *  adding, so this is the one place the badge and the boxes are editable together. */
  async function passATest() {
    apiMock.testInstance.mockResolvedValue({
      ok: true,
      detail_reason: { k: "legacy", p: { text: "Connected to Sonarr." } },
      version: "4.0.1",
    });
    apiMock.instanceRootFolders.mockResolvedValue([]);
    apiMock.plexLibraries.mockResolvedValue(LIBRARIES);
    renderWithProviders(
      <>
        <Announcer />
        <ServiceModal kind="sonarr" instance={null} onClose={vi.fn()} />
      </>,
    );
    const user = userEvent.setup();
    await fill(user, hostBox(), "10.0.0.5");
    await user.type(keyBox(), "k");
    // The button gates on `canTest`, so wait until both boxes have filled it before acting.
    const press = await screen.findByRole("button", { name: /Test connection/i });
    await waitFor(() => expect(press).toBeEnabled());
    await user.click(press);
    await waitFor(() => expect(badge()!.textContent).toContain("Passed"));
    return user;
  }

  it("goes when the address it was computed for changes", async () => {
    const user = await passATest();

    await user.type(hostBox(), "1");

    expect(badge()).toBeNull();
  });

  it("goes when the key it was computed for changes", async () => {
    // This is the sharper case: the address on screen is still the one that passed, so the
    // badge would read as current while the credential beside it is one nobody has tried.
    const user = await passATest();

    await user.type(keyBox(), "2");

    expect(badge()).toBeNull();
  });

  it("stands again for the exact credentials it was tested against", async () => {
    // Typing back to the tested value is where clearing on change and comparing against what was
    // tested differ. Nothing was retested, but the stored result is once more a statement about
    // what is in the boxes, so withholding it would understate what Reaper knows.
    const user = await passATest();
    await user.type(hostBox(), "1");
    expect(badge()).toBeNull();

    await user.type(hostBox(), "{backspace}");

    expect(badge()!.textContent).toContain("Passed");
    // One request the whole way through. The badge is re-shown here, not re-earned.
    expect(apiMock.testInstance).toHaveBeenCalledTimes(1);
  });

  it("does not vouch for an address typed while the test was still in flight", async () => {
    // The three tests above compare the held result against the boxes and pass by reading
    // `testedWith()` at either end. This one covers the case the others don't drive: the boxes
    // stay live while the request is in flight, so a fingerprint read back at success time would
    // be whatever address is on screen when the answer lands, not the one the test was actually
    // asked about. The two would then match by construction, and the badge would vouch for a
    // host nobody tried. Capturing the fingerprint at issuance instead (`onMutate`) means the
    // held result stops describing the boxes the moment they change, so the badge correctly
    // goes.
    //
    // `DiscordModal`, `NotificationsPanel` and `ServicesPanel` share this same pairing, and a
    // hygiene gate (`test_a_held_test_result_is_stamped_when_its_request_is_issued`) holds all
    // four to it.
    let land!: (r: unknown) => void;
    apiMock.testInstance.mockReturnValue(
      new Promise((resolve) => {
        land = resolve;
      }),
    );
    apiMock.instanceRootFolders.mockResolvedValue([]);
    apiMock.plexLibraries.mockResolvedValue(LIBRARIES);
    renderWithProviders(
      <>
        <Announcer />
        <ServiceModal kind="sonarr" instance={null} onClose={vi.fn()} />
      </>,
    );
    const user = userEvent.setup();
    await fill(user, hostBox(), "10.0.0.5");
    await user.type(keyBox(), "k");
    const press = await screen.findByRole("button", { name: /Test connection/i });
    await waitFor(() => expect(press).toBeEnabled());
    await user.click(press);

    // The operator keeps typing while the request is out. This uses `type`, not `fill`, because
    // the keystroke is the point here, not the value it leaves behind.
    await user.type(hostBox(), "1");
    await act(async () => {
      land({
        ok: true,
        detail_reason: { k: "legacy", p: { text: "Connected to Sonarr." } },
        version: "4.0.1",
      });
    });

    expect(badge()).toBeNull();

    // This is the second half of the proof. An absence alone would not be enough, since a result
    // that was never stored is also absent. Typing back to the address the request was actually
    // about brings the badge back, which shows a held result filed under the right fingerprint,
    // not nothing at all.
    await user.type(hostBox(), "{backspace}");

    expect(badge()!.textContent).toContain("Passed");
  });
});

describe("why 'Add service' will not act", () => {
  // The submit button is disabled on a three-field condition, so the page must name what it is
  // waiting for, or the operator presses it and nothing happens with no explanation. This is
  // not only a screen-reader concern. There is no copy for a sighted operator either without it.
  //
  // The sentence binds to the box, not to the button. A disabled button is out of the Tab
  // order, so a description hung on the button could never be reached by the operator it is for.
  const submit = () => screen.getByRole("button", { name: "Add service" });
  const blocked = () => document.querySelector("#service-blocked");

  function renderAdd() {
    apiMock.instanceRootFolders.mockResolvedValue([]);
    apiMock.plexLibraries.mockResolvedValue(LIBRARIES);
    renderWithProviders(<ServiceModal kind="sonarr" instance={null} onClose={vi.fn()} />);
    return userEvent.setup();
  }

  it("names each empty box in turn, then the connection, then the map", async () => {
    // Every case in the chain is driven, in the order it is checked, because the chain only
    // shows the first unmet case and a later one is otherwise never reached. The button stays
    // off through all of them and turns on exactly when the last sentence clears.
    //
    // Filling the three boxes alone is not enough to enable Add. A connection test and a folder
    // map are also required, so a service can never be saved at an address Reaper has never
    // reached, or with a folder map nobody made.
    const user = renderAdd();
    expect(submit()).toBeDisabled();
    expect(blocked()!.textContent).toBe("Enter a name to add this service.");

    await user.type(screen.getByLabelText("Name"), "HD");
    expect(submit()).toBeDisabled();
    expect(blocked()!.textContent).toBe("Enter a hostname or IP address to add this service.");

    await fill(user, screen.getByLabelText(/Hostname or IP/), "10.0.0.5");
    expect(submit()).toBeDisabled();
    expect(blocked()!.textContent).toBe("Enter an API key to add this service.");

    // Typing the key does not enable the button on its own. The connection still has to be
    // proved.
    await user.type(screen.getByLabelText(/^API key$/), "k");
    expect(submit()).toBeDisabled();
    expect(blocked()!.textContent).toBe("Reaper has to reach this service before you can save.");

    // Leaving the key box fires the test, which passes and hands back one unmapped folder.
    apiMock.testInstance.mockResolvedValue({
      ok: true,
      detail_reason: { k: "legacy", p: { text: "Connected to Sonarr." } },
      version: "4.0.1",
      root_folders: [{ path: "/tv", suggested_library: null }],
      seerr_services: [],
      map_error_reason: null,
    });
    await user.tab();
    await waitFor(() =>
      expect(blocked()!.textContent).toBe("Pick a Plex library for at least one folder to save."),
    );
    expect(submit()).toBeDisabled();

    // And the map is what finally clears it.
    await user.selectOptions(selectForFolder("/tv"), "TV");
    expect(submit()).toBeEnabled();
    expect(blocked()).toBeNull();
  });

  it("points the box that is empty at the sentence about it, and no other box", async () => {
    // This is a separate assertion from the text above because one region carries three
    // different complaints. A box that describes itself with the wrong one would read out the
    // sentence about a different field as its own problem. `errorOwner` exists to prevent
    // exactly this on the password row, and this test reaches the same shape here.
    const user = renderAdd();
    const name = screen.getByLabelText("Name");
    const host = screen.getByLabelText(/Hostname or IP/);
    const key = screen.getByLabelText(/^API key$/);

    expect(name).toHaveAccessibleDescription("Enter a name to add this service.");
    expect(host).toHaveAccessibleDescription("");
    expect(key).toHaveAccessibleDescription("");

    await user.type(name, "HD");

    expect(name).toHaveAccessibleDescription("");
    expect(host).toHaveAccessibleDescription("Enter a hostname or IP address to add this service.");
    expect(key).toHaveAccessibleDescription("");
  });

  it("says 'to save' on an existing service, matching what its button does", async () => {
    // The tail sentence is read off the same `editing` flag the button's label uses. An edit
    // form needs no API key, so clearing the name is the only way to block it here, and the
    // message that names the key must stay unreachable in this state.
    const user = userEvent.setup();
    renderModal(sonarr(), []);
    await user.clear(screen.getByLabelText("Name"));

    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    expect(document.querySelector("#service-blocked")!.textContent).toBe("Enter a name to save.");
  });

  it("has no accessibility violations while it is refusing", async () => {
    renderAdd();
    expect(blocked()).not.toBeNull();
    await expectNoA11yViolations();
  });
});

describe("what a failed folder read must not do", () => {
  /** The edit form for a saved *arr whose STORED address no longer answers. */
  function renderRepair(saved: Partial<Instance> = {}) {
    apiMock.instanceRootFolders.mockRejectedValue(new Error("connection refused"));
    apiMock.plexLibraries.mockResolvedValue(LIBRARIES);
    apiMock.syncPlexLibraries.mockResolvedValue(LIBRARIES);
    apiMock.updateInstance.mockResolvedValue(sonarr(saved));
    renderWithProviders(
      <>
        <Announcer />
        <ServiceModal kind="sonarr" instance={sonarr(saved)} onClose={vi.fn()} />
      </>,
    );
    return userEvent.setup();
  }

  it("lets a passing test replace the failed by-id read, instead of trapping the operator", async () => {
    // This is the flow the feature exists for: a saved Sonarr is pointed at a new address. The
    // by-id read fails because it still uses the stored address, the operator types the new
    // one, and the test passes and hands back folders. Once that happens, the never-landed
    // notice must not outrank the grid: Save must not stay off for a mapping the operator has
    // no picker to make, and the modal must not refuse to close.
    const user = renderRepair();
    expect(await screen.findByText(/couldn't read this instance's folders/i)).toBeInTheDocument();

    apiMock.testInstance.mockResolvedValue({
      ok: true,
      detail_reason: { k: "legacy", p: { text: "Connected to Sonarr." } },
      version: "4.0.1",
      root_folders: [{ path: "/tv", suggested_library: null }],
      seerr_services: [],
      map_error_reason: null,
    });
    await fill(user, screen.getByLabelText(/Hostname or IP/), "10.0.0.9");
    await user.type(screen.getByLabelText(/New API key/), "k");
    await user.tab();

    // The grid arrives, the stale notice goes, and the requirement is satisfiable.
    await waitFor(() => expect(selectForFolder("/tv")).toBeInTheDocument());
    expect(screen.queryByText(/couldn't read this instance's folders/i)).not.toBeInTheDocument();
    await user.selectOptions(selectForFolder("/tv"), "TV");
    expect(screen.getByRole("button", { name: "Save" })).toBeEnabled();
  });

  it("keeps the stored map when the test passes but the folder read fails", async () => {
    // The silent-loss case. `map_error_reason` means the probe ran and failed, so its empty
    // list is a read that never landed, not a confirmed empty one. `[]` is truthy, though, so a
    // prune that walked it would send `{}`, which the server stores as null. Without this
    // guard, the map that tells an HD copy from a 4K one apart could be erased with no
    // confirmation, while the screen shows a notice in place of the grid.
    apiMock.instanceRootFolders.mockResolvedValue([{ path: "/tv", suggested_library: null }]);
    apiMock.plexLibraries.mockResolvedValue(LIBRARIES);
    apiMock.syncPlexLibraries.mockResolvedValue(LIBRARIES);
    const saved = sonarr({ plex_library_map: { "/tv": "TV" } });
    apiMock.updateInstance.mockResolvedValue(saved);
    renderWithProviders(
      <>
        <Announcer />
        <ServiceModal kind="sonarr" instance={saved} onClose={vi.fn()} />
      </>,
    );
    const user = userEvent.setup();
    await waitFor(() => expect(selectForFolder("/tv").value).toBe("TV"));

    apiMock.testInstance.mockResolvedValue({
      ok: true,
      detail_reason: { k: "legacy", p: { text: "Connected to Sonarr." } },
      version: "4.0.1",
      root_folders: [],
      seerr_services: [],
      map_error_reason: { k: "mapError", p: { error: "It timed out." } },
    });
    await user.type(screen.getByLabelText(/New API key/), "fresh-key");
    await user.tab();
    await waitFor(() => expect(screen.getByText(/couldn't read what to map/i)).toBeInTheDocument());

    // The grid stays up over the warning, because the by-id read still holds the folders.
    expect(selectForFolder("/tv").value).toBe("TV");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(apiMock.updateInstance).toHaveBeenCalled());
    const body = apiMock.updateInstance.mock.calls[0]![1] as {
      plex_library_map?: Record<string, string>;
    };
    expect(body.plex_library_map).toEqual({ "/tv": "TV" });
  });

  it("says a test is what fills the grid, rather than claiming to be reading already", async () => {
    // The by-id query is `enabled: editing && isArr`, and a disabled query reports
    // `status: "pending"` forever. Without the fix, the add form would sit under "Reading this
    // instance's folders…" from the moment it opened, describing a read that never started, and
    // the sentence written for that exact moment could never render.
    apiMock.instanceRootFolders.mockResolvedValue([]);
    apiMock.plexLibraries.mockResolvedValue(LIBRARIES);
    apiMock.syncPlexLibraries.mockResolvedValue(LIBRARIES);
    renderWithProviders(<ServiceModal kind="sonarr" instance={null} onClose={vi.fn()} />);
    expect(
      await screen.findByText(/Your folders appear here once Reaper reaches/i),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Reading this instance's folders/i)).not.toBeInTheDocument();
  });

  it("does not demand a key when only the certificate check was flipped", async () => {
    // The switch is not an address, but it changes what a held result vouches for, so the badge
    // still goes. Demanding a fresh test for it would also demand a key, on a form whose key
    // box is blank by design, and would tell the operator they had changed an address they had
    // not touched.
    apiMock.instanceRootFolders.mockResolvedValue([{ path: "/tv", suggested_library: "TV" }]);
    apiMock.plexLibraries.mockResolvedValue(LIBRARIES);
    apiMock.syncPlexLibraries.mockResolvedValue(LIBRARIES);
    const saved = sonarr({ base_url: "https://tv.example.com", plex_library_map: { "/tv": "TV" } });
    apiMock.updateInstance.mockResolvedValue(saved);
    renderWithProviders(<ServiceModal kind="sonarr" instance={saved} onClose={vi.fn()} />);
    const user = userEvent.setup();
    await waitFor(() => expect(screen.getByRole("button", { name: "Save" })).toBeEnabled());

    await user.click(screen.getByLabelText("Check the server's certificate"));
    expect(screen.getByRole("button", { name: "Save" })).toBeEnabled();
    expect(document.querySelector("#service-blocked")).toBeNull();
  });
});
