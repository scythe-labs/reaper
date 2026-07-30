// SPDX-License-Identifier: AGPL-3.0-or-later
// The service edit modal's two maps. The HD/4K library map (Sonarr/Radarr) and the multi-Seerr
// service->instance map (Seerr) share one grammar, pinned here: each row is paired with a select,
// a suggested-but-unconfirmed pick wears a "suggested" tag that clears once they choose, saving
// sends the map they see, and a list that could not be read fails to a visible notice, never a
// silent empty list.
import { QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Instance, PlexLibrary, RootFolder, SeerrService } from "../api";
import { expectNoA11yViolations } from "../test/a11y";
import { testQueryClient } from "../test/queryClient";
import { Announcer } from "../announce";
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

  // Saving REBUILDS the map from the folders in hand, which is how an entry for a folder the *arr
  // no longer has gets dropped. That is right while the list is current and destructive when it is
  // merely out of date: a refetch that fails keeps the last good list, the grid deliberately stays
  // on screen with the stale line over it, and `.data` is truthy the whole time -- so the old
  // `.data` test could not tell the two apart and Save pruned against a list nobody had confirmed
  // (#204). The map is what tells an HD copy from a 4K one. Both directions are pinned, because a
  // fix that stopped pruning altogether would leave a folder that IS gone mapped forever.
  describe("pruning the stored map", () => {
    /** The modal with a stored map wider than the folder list that lands, keeping the client so
     *  the test can fail the refetch behind the grid. */
    function renderWithStaleableFolders() {
      const instance = sonarr({ plex_library_map: { "/tv": "TV", "/archive": "TV 4K" } });
      apiMock.instanceRootFolders.mockResolvedValue([{ path: "/tv", suggested_library: "TV" }]);
      apiMock.plexLibraries.mockResolvedValue(LIBRARIES);
      apiMock.updateInstance.mockResolvedValue(instance);
      const queryClient = testQueryClient();
      render(
        <QueryClientProvider client={queryClient}>
          <ServiceModal kind="sonarr" instance={instance} onClose={vi.fn()} />
        </QueryClientProvider>,
      );
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

      // The read succeeded, so "/archive is not in the list" is an answer, not a gap.
      expect(await savedMap()).toEqual({ "/tv": "TV" });
    });

    it("keeps it when the folder list is only out of date", async () => {
      const queryClient = renderWithStaleableFolders();
      await waitFor(() => expect(selectForFolder("/tv").value).toBe("TV"));

      apiMock.instanceRootFolders.mockRejectedValue(new Error("unreachable"));
      await act(async () => {
        await queryClient.invalidateQueries({ queryKey: ["instance-root-folders"] });
      });
      // The stale line is up and the grid is still there, which is the state the guard is about.
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

describe("what a screen reader calls the host box", () => {
  // The scheme is drawn as a prefix fused inside the field's box, and the <label> wrapped it, so
  // the box announced as "Hostname or IP http colon slash slash": the operator heard punctuation
  // where the field's job should have been (#214).
  //
  // `toHaveAccessibleName`, not `getByLabelText`, because the accessible name is the thing under
  // test and only the name computation honors `aria-hidden`. A substring lookup is what let this
  // sit here in the first place -- /Hostname or IP/ matched happily either way.
  const hostBox = () => screen.getByLabelText(/Hostname or IP/);

  it("names it for what goes in it, not for the scheme drawn beside it", async () => {
    renderModal(sonarr(), []);
    expect(hostBox()).toHaveAccessibleName("Hostname or IP");
  });

  it("keeps that name with SSL on, where the prefix is a different string", async () => {
    // Both branches of the prefix driven, since the name must not depend on which one renders
    // (rule 145). The toggle is the control that states the scheme in words, and it is reachable.
    renderModal(sonarr({ base_url: "https://10.0.0.5:8989" }), []);
    const toggle = screen.getByRole("switch", { name: /Use SSL/i });
    expect(toggle).toBeChecked();
    expect(hostBox()).toHaveAccessibleName("Hostname or IP");
  });
});

describe("what a screen reader hears when a connection is tested", () => {
  // Press "Test connection" and the result rendered as a badge that sat in no live region, at
  // any of its five sites -- so an operator using a reader learned the outcome only if they
  // happened to navigate onto it. The failure path did not speak by another route either:
  // `instances.py` never raises for a failed test, so an unreachable host arrives as a 200 with
  // `ok=False` through `onSuccess` and never reaches the shared error notice (#192). Whether
  // Reaper can reach the Sonarr it deletes THROUGH is worth hearing.
  const spoken = () =>
    [...document.querySelectorAll('[aria-live="polite"]')].map((n) => n.textContent).join("");

  /** The ADD form, because "Test connection" is only offered there -- a saved instance is
   *  tested from its Settings row instead, through `testSavedInstance`, the rule 72 sibling
   *  that got the same call. */
  async function renderWithAnnouncer() {
    apiMock.instanceRootFolders.mockResolvedValue([]);
    apiMock.plexLibraries.mockResolvedValue(LIBRARIES);
    const queryClient = testQueryClient();
    render(
      <QueryClientProvider client={queryClient}>
        <Announcer />
        <ServiceModal kind="sonarr" instance={null} onClose={vi.fn()} />
      </QueryClientProvider>,
    );
    const user = userEvent.setup();
    await user.type(screen.getByLabelText(/Hostname or IP/), "10.0.0.5");
    await user.type(screen.getByLabelText(/^API key$/), "k");
    return user;
  }

  it("says a connection was reached, and which version", async () => {
    apiMock.testInstance.mockResolvedValue({
      ok: true,
      detail: "Connected to Sonarr.",
      version: "4.0.1",
    });
    const user = await renderWithAnnouncer();

    await user.click(await screen.findByRole("button", { name: /Test connection/i }));

    // "version 4.0.1" spelled out, where the badge shows "(v4.0.1)": a reader voices a bare
    // "v" as a letter. The only deliberate difference between the two copies.
    await waitFor(() => expect(spoken()).toBe("Passed: Connected to Sonarr. (version 4.0.1)"));
  });

  it("says a connection FAILED, which arrives as an ordinary 200", async () => {
    apiMock.testInstance.mockResolvedValue({
      ok: false,
      detail: "Couldn't reach it. Check the address.",
      version: null,
    });
    const user = await renderWithAnnouncer();

    await user.click(await screen.findByRole("button", { name: /Test connection/i }));

    await waitFor(() => expect(spoken()).toBe("Failed: Couldn't reach it. Check the address."));
    // The same string the badge renders for a reader who does navigate to it (rule 144).
    expect(document.querySelector(".test-badge")?.textContent).toContain(
      "Failed: ✗ Couldn't reach it. Check the address.",
    );
  });
});

describe("what the connection badge vouches for", () => {
  // #178: `setTest` was called in exactly two places, the declaration and the mutation's
  // `onSuccess`, and NOTHING cleared it -- not the submit handler, not the Test button. So a
  // passed test followed by an edited hostname or key left the "Passed" badge on screen beside
  // credentials that had never been tried. Rule 85's family: an indicator must describe the state
  // it was computed from.
  const badge = () => document.querySelector(".test-badge");
  const hostBox = () => screen.getByLabelText(/Hostname or IP/);
  const keyBox = () => screen.getByLabelText(/^API key$/);

  /** The ADD form with a passed test already on screen -- "Test connection" is only offered while
   *  adding, so this is the one place the badge and the boxes are editable together. */
  async function passATest() {
    apiMock.testInstance.mockResolvedValue({
      ok: true,
      detail: "Connected to Sonarr.",
      version: "4.0.1",
    });
    apiMock.instanceRootFolders.mockResolvedValue([]);
    apiMock.plexLibraries.mockResolvedValue(LIBRARIES);
    render(
      <QueryClientProvider client={testQueryClient()}>
        <Announcer />
        <ServiceModal kind="sonarr" instance={null} onClose={vi.fn()} />
      </QueryClientProvider>,
    );
    const user = userEvent.setup();
    await user.type(hostBox(), "10.0.0.5");
    await user.type(keyBox(), "k");
    // Rule 137: the button gates on `canTest`, so act only once both boxes have filled it.
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
    // The sharper half: the address on screen is still the one that passed, so the badge reads as
    // current while the credential beside it is one nobody has tried.
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
    // One request the whole way through: the badge is re-shown, never re-earned.
    expect(apiMock.testInstance).toHaveBeenCalledTimes(1);
  });
});

describe("why 'Add service' will not act", () => {
  // The submit button was `disabled` on a three-field conjunction with nothing on the page naming
  // any of them: the operator pressed it, nothing happened, and the form did not say what it was
  // waiting for. Not a screen-reader gap -- there was no copy for a sighted operator either
  // (#188).
  //
  // The sentence binds to the BOX, not to the button, and that is the whole design: a `disabled`
  // button is out of the Tab order, so a description hung on it can never be reached by the
  // operator it is for.
  const submit = () => screen.getByRole("button", { name: "Add service" });
  const blocked = () => document.querySelector("#service-blocked");

  function renderAdd() {
    apiMock.instanceRootFolders.mockResolvedValue([]);
    apiMock.plexLibraries.mockResolvedValue(LIBRARIES);
    const queryClient = testQueryClient();
    render(
      <QueryClientProvider client={queryClient}>
        <ServiceModal kind="sonarr" instance={null} onClose={vi.fn()} />
      </QueryClientProvider>,
    );
    return userEvent.setup();
  }

  it("names each empty box in turn, and lets go once the form is fillable", async () => {
    // Every arm of the chain driven, in the order the boxes are on screen, because the chain
    // shows only the FIRST and a later arm is otherwise never reached (rule 145). The button
    // stays off for all three and turns on exactly when the sentence goes.
    const user = renderAdd();
    expect(submit()).toBeDisabled();
    expect(blocked()!.textContent).toBe("Enter a name to add this service.");

    await user.type(screen.getByLabelText("Name"), "HD");
    expect(submit()).toBeDisabled();
    expect(blocked()!.textContent).toBe("Enter a hostname or IP address to add this service.");

    await user.type(screen.getByLabelText(/Hostname or IP/), "10.0.0.5");
    expect(submit()).toBeDisabled();
    expect(blocked()!.textContent).toBe("Enter an API key to add this service.");

    await user.type(screen.getByLabelText(/^API key$/), "k");
    expect(submit()).toBeEnabled();
    expect(blocked()).toBeNull();
  });

  it("points the box that is empty at the sentence about it, and no other box", async () => {
    // The reason this is a separate assertion from the text: one region carries three different
    // complaints, so a box describing itself with the wrong one reads the sentence about a
    // DIFFERENT field out as its own problem -- the bug `errorOwner` was written for in the
    // password row (#174), reached here by the same shape.
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
    // The tail is read off the same `editing` the button's label is (rule 144). An edit form
    // needs no API key, so clearing the NAME is the only way to block it -- and the arm that
    // names the key must stay unreachable here.
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
