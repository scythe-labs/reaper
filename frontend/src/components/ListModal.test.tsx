// SPDX-License-Identifier: AGPL-3.0-or-later
// Adding and editing a protection list (#475).
//
// The form's job is to make a list that could never match anything impossible to save, and to
// say which box is empty while the operator is looking at it. Every refusal it renders is one
// `services.list_config._clean_config` also enforces -- these pin that the form states them,
// and `tests/test_list_config.py` pins that the server refuses them, so neither half can be
// the only thing standing between an operator and a list that protects nothing.
//
// The library picker is the #483 fix at the surface: the library stops being a name Reaper
// guessed ("Movies") and becomes one picked off the operator's own server.
import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { expectNoA11yViolations } from "../test/a11y";
import { testQueryClient } from "../test/queryClient";
import type { ListConfig } from "../api";
import { ListModal } from "./ListModal";

const { apiMock } = vi.hoisted(() => ({
  apiMock: {
    addList: vi.fn(),
    editList: vi.fn(),
    plexLibraries: vi.fn(),
  },
}));
vi.mock("../api", () => ({ api: apiMock }));

const PLEX_DEF: ListConfig = {
  id: 2,
  name: "Never Reap",
  source: "plex_collection",
  config: { library: "Films", collection: "Never Reap" },
  enabled: true,
  built_in: false,
};

function renderModal(editing: ListConfig | null = null) {
  const onClose = vi.fn();
  render(
    <QueryClientProvider client={testQueryClient()}>
      <ListModal editing={editing} onClose={onClose} />
    </QueryClientProvider>,
  );
  return { onClose };
}

beforeEach(() => {
  Object.values(apiMock).forEach((fn) => fn.mockReset());
  apiMock.plexLibraries.mockResolvedValue([
    { key: 1, title: "Films", kind: "movie", enabled: true },
    { key: 2, title: "Shows", kind: "show", enabled: true },
  ]);
  apiMock.addList.mockResolvedValue(PLEX_DEF);
  apiMock.editList.mockResolvedValue(PLEX_DEF);
});

describe("adding a list", () => {
  it("has no accessibility violations", async () => {
    renderModal();
    expect(await screen.findByLabelText("Plex library")).toBeInTheDocument();
    await expectNoA11yViolations();
  });

  it("will not save a list with no name", async () => {
    renderModal();
    expect(await screen.findByLabelText("Plex library")).toBeInTheDocument();

    expect(screen.getByRole("button", { name: "Add list" })).toBeDisabled();
    expect(screen.getByText(/Give the list a name/)).toBeInTheDocument();
  });

  it("will not save a Plex list with no collection, and says which box", async () => {
    const user = userEvent.setup();
    renderModal();
    await user.type(await screen.findByLabelText("Name"), "Keep");
    await user.selectOptions(screen.getByLabelText("Plex library"), "Films");

    expect(screen.getByRole("button", { name: "Add list" })).toBeDisabled();
    expect(screen.getByText(/Say which collection in that library to read/)).toBeInTheDocument();
  });

  it("saves the library the operator picked, not one Reaper guessed", async () => {
    // #483, at the surface. The keep collection used to be read out of a library hardcoded to
    // "Movies", so an operator whose library is called anything else had it silently never
    // read -- and, it being a HARD list, could not reap at all.
    const user = userEvent.setup();
    renderModal();
    await user.type(await screen.findByLabelText("Name"), "Keep");
    await user.selectOptions(screen.getByLabelText("Plex library"), "Shows");
    await user.type(screen.getByLabelText("Collection"), "Never Reap");
    await user.click(screen.getByRole("button", { name: "Add list" }));

    await waitFor(() =>
      expect(apiMock.addList).toHaveBeenCalledWith("Keep", "plex_collection", {
        library: "Shows",
        collection: "Never Reap",
      }),
    );
  });

  it("falls back to a typed library name when Plex cannot be read", async () => {
    // An empty select is a form that cannot be filled in, and being unable to reach Plex must
    // not be the reason an operator cannot write down which collection protects their files.
    const user = userEvent.setup();
    apiMock.plexLibraries.mockRejectedValue(new Error("nope"));
    renderModal();

    // Waited for, not read off the first paint: the box is already an input while the read is
    // in flight, so asserting on it alone would pass before Plex had failed at all (rule 137).
    expect(
      await screen.findByText(/type the name exactly as it appears in Plex/),
    ).toBeInTheDocument();
    const box = screen.getByLabelText("Plex library");
    expect(box.tagName).toBe("INPUT");

    await user.type(screen.getByLabelText("Name"), "Keep");
    await user.type(box, "Kids films");
    await user.type(screen.getByLabelText("Collection"), "Never Reap");
    await user.click(screen.getByRole("button", { name: "Add list" }));

    await waitFor(() =>
      expect(apiMock.addList).toHaveBeenCalledWith("Keep", "plex_collection", {
        library: "Kids films",
        collection: "Never Reap",
      }),
    );
  });

  it("will not save a tag list with no tags", async () => {
    const user = userEvent.setup();
    renderModal();
    await user.type(await screen.findByLabelText("Name"), "Keep");
    await user.click(screen.getByRole("button", { name: "Sonarr or Radarr tag" }));

    expect(screen.getByRole("button", { name: "Add list" })).toBeDisabled();
    expect(screen.getByText(/Add at least one tag/)).toBeInTheDocument();
  });

  it("saves the tags and how many of them a title needs", async () => {
    const user = userEvent.setup();
    renderModal();
    await user.type(await screen.findByLabelText("Name"), "Keep");
    await user.click(screen.getByRole("button", { name: "Sonarr or Radarr tag" }));
    await user.type(screen.getByLabelText("Add a tag"), "keep,gold,");
    await user.click(screen.getByRole("button", { name: "all of these tags" }));
    await user.click(screen.getByRole("button", { name: "Add list" }));

    await waitFor(() =>
      expect(apiMock.addList).toHaveBeenCalledWith("Keep", "arr_tag", {
        tags: ["keep", "gold"],
        match: "all",
      }),
    );
  });

  it("shows the server's refusal rather than a phrasing of its own", async () => {
    // Rule 144: one requirement written twice in two places is the copy that drifts from the
    // check that enforces it. The form pre-empts what it can; anything else is the server's.
    const user = userEvent.setup();
    apiMock.addList.mockRejectedValue(new Error("You already have a list with that name."));
    renderModal();
    await user.type(await screen.findByLabelText("Name"), "Keep");
    await user.selectOptions(screen.getByLabelText("Plex library"), "Films");
    await user.type(screen.getByLabelText("Collection"), "Never Reap");
    await user.click(screen.getByRole("button", { name: "Add list" }));

    expect(await screen.findByText(/You already have a list with that name\./)).toBeInTheDocument();
  });
});

describe("editing a list", () => {
  it("opens on what is stored", async () => {
    renderModal(PLEX_DEF);

    expect(await screen.findByLabelText("Name")).toHaveValue("Never Reap");
    expect(screen.getByLabelText("Plex library")).toHaveValue("Films");
    expect(screen.getByLabelText("Collection")).toHaveValue("Never Reap");
  });

  it("states where a list comes from rather than offering to change it", async () => {
    // The stored membership is keyed on a slug carrying the source, so re-pointing a list
    // would leave the old membership enabled and still protecting from a definition the
    // operator has already replaced.
    renderModal(PLEX_DEF);

    expect(await screen.findByText(/Where it comes from: Plex collection/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Sonarr or Radarr tag" })).not.toBeInTheDocument();
  });

  it("warns what switching a list off costs, before it is saved", async () => {
    const user = userEvent.setup();
    renderModal(PLEX_DEF);

    await user.click(await screen.findByRole("switch", { name: "Protecting" }));

    expect(screen.getByText(/can be deleted by the next scan/)).toBeInTheDocument();
  });

  it("keeps the shipped list's body out of the request", async () => {
    // Rule 1: omitted means "leave it". A curated list's body says WHICH shipped list it is,
    // and that is not the operator's to retype -- so a rename must not rewrite it.
    const user = userEvent.setup();
    const curated: ListConfig = {
      id: 1,
      name: "IMDb Top 250",
      source: "curated",
      config: { list: "imdb-top-250" },
      enabled: true,
      built_in: true,
    };
    renderModal(curated);

    await user.clear(await screen.findByLabelText("Name"));
    await user.type(screen.getByLabelText("Name"), "Films worth keeping");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(apiMock.editList).toHaveBeenCalledWith(1, {
        name: "Films worth keeping",
        enabled: true,
      }),
    );
  });

  it("says the shipped list cannot be pointed elsewhere or removed", async () => {
    renderModal({
      id: 1,
      name: "IMDb Top 250",
      source: "curated",
      config: { list: "imdb-top-250" },
      enabled: true,
      built_in: true,
    });

    expect(
      await screen.findByText(/cannot be pointed somewhere else or removed/),
    ).toBeInTheDocument();
  });
});
