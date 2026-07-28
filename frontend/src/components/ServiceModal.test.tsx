// SPDX-License-Identifier: AGPL-3.0-or-later
// The service edit modal's two maps. The HD/4K library map (Sonarr/Radarr) and the multi-Seerr
// service->instance map (Seerr) share one grammar, pinned here: each row is paired with a select,
// a suggested-but-unconfirmed pick wears a "suggested" tag that clears once they choose, saving
// sends the map they see, and a list that could not be read fails to a visible notice, never a
// silent empty list.
import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Instance, PlexLibrary, RootFolder, SeerrService } from "../api";
import { testQueryClient } from "../test/queryClient";
import { ServiceModal } from "./ServiceModal";

const { apiMock } = vi.hoisted(() => ({
  apiMock: {
    instanceRootFolders: vi.fn(),
    instanceSeerrServices: vi.fn(),
    instances: vi.fn(),
    plexLibraries: vi.fn(),
    updateInstance: vi.fn(),
    createInstance: vi.fn(),
    testInstance: vi.fn(),
    testSavedInstance: vi.fn(),
  },
}));

vi.mock("../api", () => ({ api: apiMock }));

// The mocks are shared across tests, so clear call history between them: otherwise a later
// test that reads `updateInstance.mock.calls[0]` sees an earlier test's save, not its own.
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
    api_path_prefix: "/api/v3",
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
  apiMock.updateInstance.mockResolvedValue(instance);
  const onClose = vi.fn();
  const queryClient = testQueryClient();
  render(
    <QueryClientProvider client={queryClient}>
      <ServiceModal kind="sonarr" instance={instance} onClose={onClose} />
    </QueryClientProvider>,
  );
  return { onClose };
}

/** The library select for one root-folder row, found by that row's folder label. */
function selectForFolder(path: string): HTMLSelectElement {
  // By accessible name, not by DOM position: the old walk from the .pl-root cell to its
  // sibling found these selects while every one of them was nameless (#147), so it could
  // not have told us they were. Reaching them the way a screen reader does keeps that
  // regression red instead of invisible.
  return screen.getByLabelText(`Plex library for ${path}`) as HTMLSelectElement;
}

describe("ServiceModal HD/4K library map", () => {
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
    // The suggested folder is included; the unset one is dropped, not stored as "".
    expect(body.plex_library_map).toEqual({ "/tv": "TV" });
  });

  it("keeps a saved mapping and does not tag it 'suggested'", async () => {
    renderModal(sonarr({ plex_library_map: { "/tv": "TV" } }), [
      { path: "/tv", suggested_library: "TV" },
    ]);
    await waitFor(() => expect(selectForFolder("/tv").value).toBe("TV"));
    // A folder already saved is not a suggestion, so no tag.
    expect(screen.queryByText("suggested")).not.toBeInTheDocument();
  });

  it("shows a notice, not an empty list, when the folders can't be read", async () => {
    renderModal(sonarr(), new Error("unreachable"));
    expect(await screen.findByText(/couldn't read this instance's folders/i)).toBeInTheDocument();
  });

  it("does not claim the operator has no libraries when the list can't be read", async () => {
    // B-20: a failed fetch empties the options exactly as a genuinely-empty library list does,
    // and the "none yet" sentence then states as fact something Reaper never learned -- and
    // sends the operator off to re-sync a list that is already there.
    renderModal(sonarr(), [{ path: "/tv", suggested_library: "TV" }], new Error("unreachable"));
    expect(await screen.findByText(/couldn't read your Plex libraries/i)).toBeInTheDocument();
    expect(screen.queryByText(/No Plex libraries yet/i)).not.toBeInTheDocument();
  });

  it("keeps the 'none yet' sentence for a list that really is empty", async () => {
    renderModal(sonarr(), [{ path: "/tv", suggested_library: null }], []);
    expect(await screen.findByText(/No Plex libraries yet/i)).toBeInTheDocument();
    expect(screen.queryByText(/couldn't read your Plex libraries/i)).not.toBeInTheDocument();
  });

  it("names every picker after its own folder", async () => {
    // #147: the folder is in a sibling cell the select is not labeled by, so every row
    // announced as "combobox, Not set" and an operator on a screen reader could not tell
    // which folder they were mapping. Picking the wrong one aims Leaving Soon writes and
    // the Never-Reap read at a library Reaper was never meant to touch.
    renderModal(sonarr(), [
      { path: "/tv", suggested_library: "TV" },
      { path: "/tv-4k", suggested_library: "TV 4K" },
    ]);
    await waitFor(() => expect(selectForFolder("/tv-4k").value).toBe("TV 4K"));
    // Every combobox on the panel has a name, and no two rows share one.
    const named = screen.getAllByRole("combobox").map((c) => c.getAttribute("aria-label"));
    expect(named).toEqual(["Plex library for /tv", "Plex library for /tv-4k"]);
    expect(screen.queryAllByRole("combobox", { name: "" })).toHaveLength(0);
    // The spoken name is derived from the folder cell, and every helper in this file now
    // reaches these selects through that name -- so nothing else here observes that the cell
    // renders at all. Empty the column and each assertion above still passes, which is the
    // same blindness #147 was hiding in, one layer over. Rule 118.
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
    // S-5: a scheme-less paste is caught before save (mirroring the server's 422), so the value
    // is never sent to be stored verbatim.
    renderModal(sonarr({ external_url: null }), []);
    await userEvent.type(screen.getByLabelText("External URL"), "tv.example.com:8989");

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
  const queryClient = testQueryClient();
  render(
    <QueryClientProvider client={queryClient}>
      <ServiceModal kind="seerr" instance={instance} onClose={vi.fn()} />
    </QueryClientProvider>,
  );
}

/** The instance select for one service row, found by that row's service name and media kind. */
function selectForService(name: string, media: "TV" | "Movies" = "TV"): HTMLSelectElement {
  // By accessible name, for the reason given on selectForFolder above. The media kind is part
  // of that name because it is part of the row's identity: two services can share a name.
  return screen.getByLabelText(`Connection for ${name}, ${media}`) as HTMLSelectElement;
}

describe("ServiceModal multi-Seerr service map", () => {
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
    // The mapped service is sent keyed by "{kind}:{serviceId}"; the unset one is dropped.
    expect(body.service_instance_map).toEqual({ "sonarr:2": 3 });
  });

  it("does not collide a Sonarr and a Radarr service that share a serviceId", async () => {
    // Seerr numbers Sonarr and Radarr services separately, so both have a serviceId 0. Each row
    // must prefill its OWN suggestion; the movie row must not read the tv row's value.
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
    // #147 on this grid. A portal routinely carries an HD and a 4K service under one name,
    // so the tag is the only thing telling those two rows apart -- it has to be spoken, not
    // just shown.
    renderSeerrModal(seerr(), [
      { service_id: 2, kind: "sonarr", name: "Main TV", is_4k: false, suggested_instance_id: 3 },
      { service_id: 5, kind: "sonarr", name: "Main TV", is_4k: true, suggested_instance_id: null },
    ]);
    await waitFor(() => expect(selectForService("Main TV").value).toBe("3"));
    const named = screen.getAllByRole("combobox").map((c) => c.getAttribute("aria-label"));
    expect(named).toEqual(["Connection for Main TV, TV", "Connection for Main TV 4K, TV"]);
    expect(screen.queryAllByRole("combobox", { name: "" })).toHaveLength(0);
    // As on the folder grid: both helpers reach these selects by their spoken name, so the
    // visible cell they are derived from is otherwise unobserved. Rule 118.
    expect(screen.getAllByText("Main TV")).toHaveLength(2);
    expect(screen.getByText("4K")).toBeInTheDocument();
  });

  it("names a TV and a Movies service apart when the portal gives them one name", async () => {
    // The row's identity is kind + id, so the name has to carry the kind too. Seerr numbers
    // and names the two lists independently, so one portal can hold a TV and a Movies service
    // both called "Media" -- with the name alone those two rows are announced identically and
    // the operator maps their movie requests onto a TV connection.
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

  it("shows a notice, not an empty list, when the services can't be read", async () => {
    renderSeerrModal(seerr(), new Error("forbidden"));
    expect(await screen.findByText(/couldn't read this portal's services/i)).toBeInTheDocument();
  });

  it("does not claim there are no Sonarr or Radarr connections when the list can't be read", async () => {
    // B-20, the same trap on the other picker: every `instanceOptions` comes back empty.
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
    // B-19: the scrim, Escape and ✕ used to tear the modal down mid-save. A 409 "a service with
    // that name already exists" then landed on an unmounted component, the caches were never
    // invalidated, and the operator walked away believing the change had saved.
    const { onClose } = renderModal(sonarr(), [{ path: "/tv", suggested_library: "TV" }]);
    apiMock.updateInstance.mockReturnValue(new Promise(() => {})); // in flight, forever
    await userEvent.click(await screen.findByRole("button", { name: "Save" }));

    await waitFor(() => expect(screen.getByRole("button", { name: "Saving…" })).toBeDisabled());
    expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
    await userEvent.keyboard("{Escape}");
    expect(onClose).not.toHaveBeenCalled();
  });
});
