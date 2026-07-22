// SPDX-License-Identifier: AGPL-3.0-or-later
// The service edit modal's two maps. The HD/4K library map (Sonarr/Radarr) and the multi-Seerr
// service->instance map (Seerr) share one grammar, pinned here: each row is paired with a select,
// a suggested-but-unconfirmed pick wears a "suggested" tag that clears once they choose, saving
// sends the map they see, and a list that could not be read fails to a visible notice, never a
// silent empty list.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Instance, PlexLibrary, RootFolder, SeerrService } from "../api";
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
  return { ...sonarr(), id: 5, kind: "seerr", name: "Primary", base_url: "http://10.0.0.9:5055", ...overrides };
}

const LIBRARIES: PlexLibrary[] = [
  { key: 1, title: "TV", kind: "show", enabled: true },
  { key: 2, title: "TV 4K", kind: "show", enabled: true },
  { key: 3, title: "Movies", kind: "movie", enabled: true },
];

function renderModal(
  instance: Instance,
  folders: RootFolder[] | Error,
  libraries: PlexLibrary[] = LIBRARIES,
) {
  if (folders instanceof Error) apiMock.instanceRootFolders.mockRejectedValue(folders);
  else apiMock.instanceRootFolders.mockResolvedValue(folders);
  apiMock.plexLibraries.mockResolvedValue(libraries);
  apiMock.updateInstance.mockResolvedValue(instance);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <ServiceModal kind="sonarr" instance={instance} onClose={vi.fn()} />
    </QueryClientProvider>,
  );
}

/** The library select for one root-folder row, found by that row's folder label. */
function selectForFolder(path: string): HTMLSelectElement {
  // The grid is flat: each .pl-root cell is immediately followed by its row's .pl-pick cell.
  const root = screen.getByText(path);
  const pick = root.nextElementSibling as HTMLElement;
  return within(pick).getByRole("combobox") as HTMLSelectElement;
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
    expect(
      await screen.findByText(/couldn't read this instance's folders/i),
    ).toBeInTheDocument();
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
});

const ARR_INSTANCES: Instance[] = [
  sonarr({ id: 3, kind: "sonarr", name: "Main Sonarr" }),
  sonarr({ id: 4, kind: "sonarr", name: "Restricted Sonarr" }),
  sonarr({ id: 7, kind: "radarr", name: "Main Radarr" }),
];

function renderSeerrModal(
  instance: Instance,
  services: SeerrService[] | Error,
  arrs: Instance[] = ARR_INSTANCES,
) {
  if (services instanceof Error) apiMock.instanceSeerrServices.mockRejectedValue(services);
  else apiMock.instanceSeerrServices.mockResolvedValue(services);
  apiMock.instances.mockResolvedValue(arrs);
  apiMock.updateInstance.mockResolvedValue(instance);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <ServiceModal kind="seerr" instance={instance} onClose={vi.fn()} />
    </QueryClientProvider>,
  );
}

/** The instance select for one service row, found by that row's service name. */
function selectForService(name: string): HTMLSelectElement {
  const root = screen.getByText(name);
  const pick = root.nextElementSibling as HTMLElement;
  return within(pick).getByRole("combobox") as HTMLSelectElement;
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
      { service_id: 9, kind: "sonarr", name: "Unmapped", is_4k: false, suggested_instance_id: null },
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
    expect(selectForService("HD Movies").value).toBe("7"); // not "Not set", not "3"
    expect(screen.getAllByText("suggested")).toHaveLength(2);
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(apiMock.updateInstance).toHaveBeenCalled());
    const body = apiMock.updateInstance.mock.calls[0]![1] as {
      service_instance_map?: Record<string, number>;
    };
    expect(body.service_instance_map).toEqual({ "sonarr:0": 3, "radarr:0": 7 });
  });

  it("shows a notice, not an empty list, when the services can't be read", async () => {
    renderSeerrModal(seerr(), new Error("forbidden"));
    expect(
      await screen.findByText(/couldn't read this portal's services/i),
    ).toBeInTheDocument();
  });
});
