// SPDX-License-Identifier: AGPL-3.0-or-later
// The service edit modal's HD/4K library map. These pin the behavior an operator relies on:
// each root folder is paired with a library select, a suggested-but-unconfirmed pick wears a
// "suggested" tag that clears once they choose, saving sends the map they see, and a folder
// list that could not be read fails to a visible notice, never a silent empty list.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { Instance, PlexLibrary, RootFolder } from "../api";
import { ServiceModal } from "./ServiceModal";

const { apiMock } = vi.hoisted(() => ({
  apiMock: {
    instanceRootFolders: vi.fn(),
    plexLibraries: vi.fn(),
    updateInstance: vi.fn(),
    createInstance: vi.fn(),
    testInstance: vi.fn(),
    testSavedInstance: vi.fn(),
  },
}));

vi.mock("../api", () => ({ api: apiMock }));

function sonarr(overrides: Partial<Instance> = {}): Instance {
  return {
    id: 3,
    kind: "sonarr",
    name: "Main",
    base_url: "http://10.0.0.5:8989",
    enabled: true,
    verify_tls: true,
    add_import_exclusion: false,
    plex_library_map: {},
    has_key: true,
    api_path_prefix: "/api/v3",
    detected_version: null,
    last_ok_at: null,
    last_error: null,
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
