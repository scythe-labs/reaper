// SPDX-License-Identifier: AGPL-3.0-or-later
// Adding and editing a protection list.
//
// Adding walks the Arr-style two-step flow: a type picker, then the one form that type needs,
// and each form's job is to make a list that could never match anything impossible to save --
// saying which box is empty while the operator is looking at it. Every refusal it renders is
// one `services.list_config._clean_config` also enforces; these pin that the form states them,
// and `tests/test_list_config.py` pins that the server refuses them, so neither half can be
// the only thing standing between an operator and a list that protects nothing.
//
// The library picker is the #483 fix at the surface: the library stops being a name Reaper
// guessed ("Movies") and becomes one picked off the operator's own server.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { expectNoA11yViolations } from "../test/a11y";
import { renderWithProviders } from "../test/renderWithProviders";
import type { ListConfig } from "../api";
import { ListModal } from "./ListModal";

const { apiMock } = await vi.hoisted(async () => ({
  apiMock: (await import("../test/apiMock")).makeApiMock(),
}));
vi.mock("../api", () => ({ api: apiMock }));

const PLEX_DEF: ListConfig = {
  id: 2,
  name: "Never Reap",
  source: "plex_collection",
  config: { library: "Films", collection: "Never Reap" },
  policy_use: [],
};

function renderModal(editing: ListConfig | null = null) {
  const onClose = vi.fn();
  const onSaved = vi.fn();
  const onChanged = vi.fn();
  renderWithProviders(
    <ListModal editing={editing} onClose={onClose} onSaved={onSaved} onChanged={onChanged} />,
  );
  return { onClose, onSaved, onChanged };
}

/** Through the picker to one type's form. Adding always starts on the picker now. */
async function openForm(user: ReturnType<typeof userEvent.setup>, card: RegExp | string) {
  const handles = renderModal();
  await user.click(await screen.findByRole("button", { name: card }));
  return handles;
}

beforeEach(() => {
  Object.values(apiMock).forEach((fn) => fn.mockReset());
  apiMock.plexLibraries.mockResolvedValue([
    { key: 1, title: "Films", kind: "movie", enabled: true },
    { key: 2, title: "Shows", kind: "show", enabled: true },
  ]);
  apiMock.syncPlexLibraries.mockResolvedValue([]);
  apiMock.addList.mockResolvedValue(PLEX_DEF);
  apiMock.editList.mockResolvedValue(PLEX_DEF);
});

describe("the type picker", () => {
  it("offers every source, grouped by service, and has no accessibility violations", async () => {
    renderModal();

    expect(await screen.findByRole("heading", { name: "Add a list" })).toBeInTheDocument();
    expect(screen.getByText("Collection")).toBeInTheDocument();
    expect(screen.getByText("Watchlist")).toBeInTheDocument();
    expect(screen.getByText("Tags")).toBeInTheDocument();
    expect(screen.getByText("IMDb list")).toBeInTheDocument();
    await expectNoA11yViolations();
  });

  it("reveals the shipped charts behind Presets, as the app's anchored menu", async () => {
    // The one menu grammar (`FilterMenu`, rule 18): the same popover the queue's filter
    // pickers render, not a second row of card buttons.
    const user = userEvent.setup();
    renderModal();

    expect(screen.queryByRole("button", { name: "IMDb Top 250" })).not.toBeInTheDocument();
    await user.click(await screen.findByRole("button", { name: "Presets" }));

    const menu = screen.getByRole("list", { name: "Presets" });
    expect(menu).toHaveClass("filter-menu");
    expect(within(menu).getByRole("button", { name: "IMDb Top 250" })).toBeInTheDocument();
    expect(within(menu).getByRole("button", { name: "IMDb Popular Movies" })).toBeInTheDocument();
  });

  it("Escape closes the menu and leaves the modal standing", async () => {
    // The menu is the newest layer, so it consumes the press; without the stop the same
    // Escape reaches ModalShell's window listener and tears the whole modal down.
    const user = userEvent.setup();
    const { onClose } = renderModal();
    await user.click(await screen.findByRole("button", { name: "Presets" }));
    expect(screen.getByRole("list", { name: "Presets" })).toBeInTheDocument();

    await user.keyboard("{Escape}");

    expect(screen.queryByRole("list", { name: "Presets" })).not.toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
  });

  it("a click outside the card closes the menu", async () => {
    const user = userEvent.setup();
    renderModal();
    await user.click(await screen.findByRole("button", { name: "Presets" }));
    expect(screen.getByRole("list", { name: "Presets" })).toBeInTheDocument();

    await user.click(screen.getByRole("heading", { name: "Add a list" }));

    expect(screen.queryByRole("list", { name: "Presets" })).not.toBeInTheDocument();
  });
});

describe("adding a Plex collection", () => {
  it("opens the form from the picker, named for the type", async () => {
    const user = userEvent.setup();
    await openForm(user, "Add a Plex collection");

    expect(
      await screen.findByRole("heading", { name: "Add a list: Plex collection" }),
    ).toBeInTheDocument();
    expect(await screen.findByLabelText("Plex library")).toBeInTheDocument();
  });

  it("will not save a list with no name", async () => {
    const user = userEvent.setup();
    await openForm(user, "Add a Plex collection");
    expect(await screen.findByLabelText("Plex library")).toBeInTheDocument();

    expect(screen.getByRole("button", { name: "Add list" })).toBeDisabled();
    expect(screen.getByText(/Give the list a name/)).toBeInTheDocument();
  });

  it("will not save one with no collection, and says which box", async () => {
    const user = userEvent.setup();
    await openForm(user, "Add a Plex collection");
    await user.type(await screen.findByLabelText("Name"), "Keep");
    await user.selectOptions(await screen.findByLabelText("Plex library"), "Films");

    expect(screen.getByRole("button", { name: "Add list" })).toBeDisabled();
    expect(screen.getByText(/Say which collection in that library to read/)).toBeInTheDocument();
  });

  it("saves the library the operator picked, not one Reaper guessed", async () => {
    // #483, at the surface. The keep collection used to be read out of a library hardcoded to
    // "Movies", so an operator whose library is called anything else had it silently never
    // read -- and, it being a HARD list, could not reap at all.
    const user = userEvent.setup();
    await openForm(user, "Add a Plex collection");
    await user.type(await screen.findByLabelText("Name"), "Keep");
    await user.selectOptions(await screen.findByLabelText("Plex library"), "Shows");
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
    await openForm(user, "Add a Plex collection");

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

  it("shows the server's refusal rather than a phrasing of its own", async () => {
    // Rule 144: one requirement written twice in two places is the copy that drifts from the
    // check that enforces it. The form pre-empts what it can; anything else is the server's.
    const user = userEvent.setup();
    apiMock.addList.mockRejectedValue(new Error("You already have a list with that name."));
    await openForm(user, "Add a Plex collection");
    await user.type(await screen.findByLabelText("Name"), "Keep");
    await user.selectOptions(await screen.findByLabelText("Plex library"), "Films");
    await user.type(screen.getByLabelText("Collection"), "Never Reap");
    await user.click(screen.getByRole("button", { name: "Add list" }));

    expect(await screen.findByText(/You already have a list with that name\./)).toBeInTheDocument();
  });
});

describe("adding a Plex watchlist", () => {
  it("needs nothing but a name, and says so", async () => {
    const user = userEvent.setup();
    await openForm(user, "Add a Plex watchlist");

    expect(
      await screen.findByText(/Reaper reads the watchlist of the Plex account it is signed in/),
    ).toBeInTheDocument();
    // The name starts filled, so the one box on the form is already valid.
    expect(screen.getByLabelText("Name")).toHaveValue("My watchlist");

    await user.click(screen.getByRole("button", { name: "Add list" }));
    await waitFor(() =>
      expect(apiMock.addList).toHaveBeenCalledWith("My watchlist", "plex_watchlist", {}),
    );
  });
});

describe("what a save hands back", () => {
  it("names the stored row, so the list is checked without a second press", async () => {
    // The panel runs the check, and it needs the id, which only the server's row carries. A
    // list nobody has read protects nothing, so a save that ends with the operator hunting
    // for Check now is a list left unprotecting for as long as they take to find it.
    const user = userEvent.setup();
    const { onSaved, onClose } = await openForm(user, "Add a Plex watchlist");

    await user.click(await screen.findByRole("button", { name: "Add list" }));

    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(PLEX_DEF));
    expect(onClose).toHaveBeenCalled();
  });

  it("does the same for an edit, which re-points what the list reads", async () => {
    // An edit changes where the membership comes from, so the stored copy is the old
    // definition's until something re-reads it (rule 72: one save, both paths).
    const user = userEvent.setup();
    const { onSaved } = renderModal(PLEX_DEF);

    await user.clear(await screen.findByLabelText("Name"));
    await user.type(screen.getByLabelText("Name"), "Films worth keeping");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(PLEX_DEF));
  });

  it("hands nothing back when the save was refused", async () => {
    // A check of a list that was not stored would report on the definition it replaced, or
    // on nothing at all, and the row would say a check ran for an edit that never landed.
    const user = userEvent.setup();
    apiMock.editList.mockRejectedValue(new Error("You already have a list with that name."));
    const { onSaved, onClose } = renderModal(PLEX_DEF);

    await user.click(await screen.findByRole("button", { name: "Save" }));

    expect(await screen.findByText(/You already have a list with that name\./)).toBeInTheDocument();
    expect(onSaved).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
  });
});

describe("adding a tag list", () => {
  it("will not save one with no tags", async () => {
    const user = userEvent.setup();
    await openForm(user, "Add a tag list");
    await user.type(await screen.findByLabelText("Name"), "Keep");

    expect(screen.getByRole("button", { name: "Add list" })).toBeDisabled();
    expect(screen.getByText(/Add at least one tag/)).toBeInTheDocument();
  });

  it("shows the any/all choice from the start, and defaults to any: the wider net", async () => {
    const user = userEvent.setup();
    await openForm(user, "Add a tag list");

    // Visible before a single tag is typed, so the form never has a blank where a control
    // belongs. It is the shared `Segmented` wearing the mockup's flat chrome, not a second
    // either-or control: this asserted a `.seg2` of its own, which is exactly what rules 18
    // and 41 refuse.
    const toggle = await screen.findByRole("group", {
      name: "How many of these tags a title needs",
    });
    expect(toggle.classList.contains("segmented")).toBe(true);
    expect(toggle.classList.contains("flat")).toBe(true);
    // Each half reserves its own bold width through `data-label` (the strut in 04-buttons.css),
    // so choosing one bolds it in place instead of widening it and shoving its neighbor
    // sideways out from under the cursor.
    for (const half of ["Any of these", "All of these"]) {
      expect(screen.getByRole("button", { name: half })).toHaveAttribute("data-label", half);
    }
    expect(screen.getByRole("button", { name: "Any of these" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );

    await user.type(screen.getByLabelText("Name"), "Keep");
    await user.type(screen.getByLabelText("Add a tag"), "keep,");
    await user.click(screen.getByRole("button", { name: "Add list" }));

    await waitFor(() =>
      expect(apiMock.addList).toHaveBeenCalledWith("Keep", "arr_tag", {
        tags: ["keep"],
        match: "any",
      }),
    );
  });

  it("saves the tags and the ALL choice when every tag is required", async () => {
    const user = userEvent.setup();
    await openForm(user, "Add a tag list");
    await user.type(await screen.findByLabelText("Name"), "Keep");
    await user.type(screen.getByLabelText("Add a tag"), "keep,gold,");
    await user.click(screen.getByRole("button", { name: "All of these" }));
    await user.click(screen.getByRole("button", { name: "Add list" }));

    await waitFor(() =>
      expect(apiMock.addList).toHaveBeenCalledWith("Keep", "arr_tag", {
        tags: ["keep", "gold"],
        match: "all",
      }),
    );
  });

  it("takes one tag once however it is capitalized", async () => {
    // Sonarr and Radarr lower-case every label, so "Keep" and "keep" are one tag there. Both
    // chips saved left the list reporting a count of zero against the spelling that lost, on
    // the screen whose subject is whether a list is protecting anything (#509).
    const user = userEvent.setup();
    await openForm(user, "Add a tag list");
    await user.type(await screen.findByLabelText("Name"), "Keep");
    await user.type(screen.getByLabelText("Add a tag"), "Keep,keep,KEEP,gold,");

    expect(screen.queryByText("keep")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Remove Keep")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Add list" }));

    await waitFor(() =>
      expect(apiMock.addList).toHaveBeenCalledWith("Keep", "arr_tag", {
        tags: ["Keep", "gold"],
        match: "any",
      }),
    );
  });
});

describe("adding an IMDb list", () => {
  it("takes a custom list by id, and will not save an empty one", async () => {
    const user = userEvent.setup();
    await openForm(user, "Custom");
    await user.type(await screen.findByLabelText("Name"), "Films worth keeping");

    expect(screen.getByRole("button", { name: "Add list" })).toBeDisabled();
    expect(screen.getByText(/Paste the list's id or URL/)).toBeInTheDocument();

    await user.type(screen.getByLabelText("List id or URL"), "ls005421403");
    await user.click(screen.getByRole("button", { name: "Add list" }));

    await waitFor(() =>
      expect(apiMock.addList).toHaveBeenCalledWith("Films worth keeping", "imdb", {
        list_id: "ls005421403",
      }),
    );
  });

  it("adds the Top 250 preset with its config key, named after the chart", async () => {
    const user = userEvent.setup();
    renderModal();
    await user.click(await screen.findByRole("button", { name: "Presets" }));
    await user.click(screen.getByRole("button", { name: "IMDb Top 250" }));

    expect(await screen.findByText(/The IMDb Top 250 preset\./)).toBeInTheDocument();
    expect(screen.getByLabelText("Name")).toHaveValue("IMDb Top 250");

    await user.click(screen.getByRole("button", { name: "Add list" }));
    await waitFor(() =>
      expect(apiMock.addList).toHaveBeenCalledWith("IMDb Top 250", "imdb", { preset: "top250" }),
    );
  });

  it("adds the Popular Movies preset with its own key", async () => {
    const user = userEvent.setup();
    renderModal();
    await user.click(await screen.findByRole("button", { name: "Presets" }));
    await user.click(screen.getByRole("button", { name: "IMDb Popular Movies" }));

    await user.click(await screen.findByRole("button", { name: "Add list" }));
    await waitFor(() =>
      expect(apiMock.addList).toHaveBeenCalledWith("IMDb Popular Movies", "imdb", {
        preset: "popular",
      }),
    );
  });
});

describe("editing a list", () => {
  it("opens on what is stored, with no picker step", async () => {
    renderModal(PLEX_DEF);

    expect(await screen.findByLabelText("Name")).toHaveValue("Never Reap");
    expect(screen.getByLabelText("Plex library")).toHaveValue("Films");
    expect(screen.getByLabelText("Collection")).toHaveValue("Never Reap");
    expect(screen.queryByRole("heading", { name: "Add a list" })).not.toBeInTheDocument();
  });

  it("states where a list comes from rather than offering to change it", async () => {
    // The stored membership is keyed on a slug carrying the source, so re-pointing a list
    // would leave the old membership enabled and still protecting from a definition the
    // operator has already replaced.
    renderModal(PLEX_DEF);

    expect(await screen.findByText(/Where it comes from: Plex collection/)).toBeInTheDocument();
    expect(screen.queryByText("Tags")).not.toBeInTheDocument();
  });

  it("has no on/off switch: a list protects while it exists", async () => {
    renderModal(PLEX_DEF);

    expect(await screen.findByLabelText("Name")).toBeInTheDocument();
    expect(screen.queryByRole("switch")).not.toBeInTheDocument();
  });

  it("sends only the name when only the name changed", async () => {
    // `ListConfigPatch` is omitted-means-keep, and this form seeds ONCE from a
    // `lists-configured` row the cache may have held for up to its 30s staleTime with focus
    // refetching off. Sending `config` back unchanged is still a WRITE of a value read
    // minutes ago, so a rename here silently reverted a collection another admin had
    // repointed in the meantime (the `cached-value-drives-a-write` shape of #203/#204).
    const user = userEvent.setup();
    renderModal(PLEX_DEF);

    await user.clear(await screen.findByLabelText("Name"));
    await user.type(screen.getByLabelText("Name"), "Films worth keeping");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(apiMock.editList).toHaveBeenCalledWith(2, { name: "Films worth keeping" }),
    );
  });

  it("sends the config when a config field changed", async () => {
    // The other half, and the reason this is a dirty check rather than "never send config":
    // an edit to the collection has to reach the server.
    const user = userEvent.setup();
    renderModal(PLEX_DEF);

    await user.clear(await screen.findByLabelText("Collection"));
    await user.type(screen.getByLabelText("Collection"), "Keep these");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(apiMock.editList).toHaveBeenCalledWith(2, {
        name: "Never Reap",
        config: { library: "Films", collection: "Keep these" },
      }),
    );
  });

  it("tells the panel to rescan when a keep rule names the edited list", async () => {
    // Editing a used list can move its membership or re-spell its rule, so a fate can change
    // and the queue must re-score. The modal only signals it; the panel starts the scan.
    const user = userEvent.setup();
    apiMock.editList.mockResolvedValue({
      ...PLEX_DEF,
      name: "Films worth keeping",
      policy_use: [{ media_type: "movie", strength: "hard", points: null }],
    });
    const { onChanged } = renderModal(PLEX_DEF);

    await user.clear(await screen.findByLabelText("Name"));
    await user.type(screen.getByLabelText("Name"), "Films worth keeping");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(onChanged).toHaveBeenCalledWith(true));
  });

  it("does not signal a rescan for a list no rule names", async () => {
    // PLEX_DEF carries no policy_use, so editing it moves no fate: the panel must not scan the
    // whole library. This is what keeps a plain add -- which also names no rule -- from scanning.
    const user = userEvent.setup();
    const { onChanged } = renderModal(PLEX_DEF);

    await user.clear(await screen.findByLabelText("Name"));
    await user.type(screen.getByLabelText("Name"), "Films worth keeping");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(onChanged).toHaveBeenCalledWith(false));
  });

  it("stops the name at the length the server refuses", async () => {
    // The schema's `max_length` is checked before `_clean_name` runs, so its sentence
    // ("That name is too long. Keep it under 100 characters.") could never fire and the
    // operator met Pydantic's "String should have at most 100 characters" instead (rule 21).
    // Reachable by paste, since nobody types 101 characters.
    renderModal(PLEX_DEF);

    expect(await screen.findByLabelText("Name")).toHaveAttribute("maxLength", "100");
  });
});

describe("removing a list, from inside Edit", () => {
  it("asks first, saying the keep rules go with it, and only removes on confirm", async () => {
    const user = userEvent.setup();
    renderModal(PLEX_DEF);

    await user.click(await screen.findByRole("button", { name: "Remove list…" }));

    expect(await screen.findByText(/can be deleted by the next scan/)).toBeInTheDocument();
    expect(screen.getByText(/Its keep rules on Policy go with it\./)).toBeInTheDocument();
    expect(apiMock.removeList).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Remove list" }));
    await waitFor(() => expect(apiMock.removeList).toHaveBeenCalledWith(2));
  });

  it("goes back to the form on Cancel, keeping the list", async () => {
    const user = userEvent.setup();
    renderModal(PLEX_DEF);

    await user.click(await screen.findByRole("button", { name: "Remove list…" }));
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(await screen.findByLabelText("Name")).toHaveValue("Never Reap");
    expect(apiMock.removeList).not.toHaveBeenCalled();
  });

  it("keeps the list when the removal is refused, and says why", async () => {
    const user = userEvent.setup();
    apiMock.removeList.mockRejectedValue(new Error("That list no longer exists. Reload the page."));
    renderModal(PLEX_DEF);

    await user.click(await screen.findByRole("button", { name: "Remove list…" }));
    await user.click(screen.getByRole("button", { name: "Remove list" }));

    expect(await screen.findByText(/That list no longer exists\./)).toBeInTheDocument();
  });
});
