// SPDX-License-Identifier: AGPL-3.0-or-later
// The General panel has one save affordance (rule 43): a bar that names every unsaved field and
// sends them together. What still has to hold is that the controls saving on the spot -- the two
// Switches, the theme and expand-seasons selects -- never throw away text being typed elsewhere.
import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { GeneralSettings } from "../api";
import { testQueryClient } from "../test/queryClient";
import { GeneralPanel } from "./Settings";

const { apiMock } = vi.hoisted(() => ({
  apiMock: { general: vi.fn(), saveGeneral: vi.fn(), revealApiKey: vi.fn() },
}));

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  api: apiMock,
}));

const STORED: GeneralSettings = {
  application_name: "Reaper",
  application_url: null,
  timezone: "UTC",
  accent_color: "#38bdf8",
  api_key_set: false,
  expand_seasons_mode: "off",
  default_spare_days: 0,
  proxy_trust_enabled: false,
  trusted_proxies: [],
};

beforeEach(() => {
  vi.clearAllMocks();
  apiMock.general.mockResolvedValue(STORED);
  // The server answers with the canonical stored row, which for the fields this save did not
  // touch is the OLD value -- that is exactly what used to overwrite the operator's typing.
  apiMock.saveGeneral.mockImplementation((body: Partial<GeneralSettings>) =>
    Promise.resolve({ ...STORED, ...body }),
  );
});

function renderPanel() {
  const queryClient = testQueryClient();
  render(
    <QueryClientProvider client={queryClient}>
      <GeneralPanel />
    </QueryClientProvider>,
  );
  return userEvent.setup();
}

const saveChanges = () => screen.getByRole("button", { name: "Save changes" });
const bar = () => document.querySelector(".savebar");

describe("the save bar", () => {
  it("is absent until something is unsaved, and names what is", async () => {
    const person = renderPanel();
    const name = await screen.findByLabelText("Application name");
    expect(bar()).toBeNull();

    await person.clear(name);
    await person.type(name, "Second install");

    await waitFor(() => expect(bar()).not.toBeNull());
    expect(bar()!.textContent).toContain("Application name");
    // A field that was never touched is not claimed as unsaved.
    expect(bar()!.textContent).not.toContain("Time zone");
  });

  it("sends every unsaved field in one request", async () => {
    const person = renderPanel();
    const url = await screen.findByLabelText("Application URL");
    const name = screen.getByLabelText("Application name");

    await person.type(url, "https://reaper.example.com");
    await person.clear(name);
    await person.type(name, "Second install");
    await person.click(saveChanges());

    await waitFor(() => expect(apiMock.saveGeneral).toHaveBeenCalledTimes(1));
    // The body only, not React Query's trailing context argument.
    expect(apiMock.saveGeneral.mock.calls[0]![0]).toEqual({
      application_name: "Second install",
      application_url: "https://reaper.example.com",
    });
    // Everything it sent is saved, so the bar has nothing left to offer.
    await waitFor(() => expect(bar()).toBeNull());
  });

  it("puts every draft back on Discard, and sends nothing", async () => {
    const person = renderPanel();
    const name = await screen.findByLabelText("Application name");
    const url = screen.getByLabelText("Application URL");

    await person.clear(name);
    await person.type(name, "Second install");
    await person.type(url, "https://reaper.example.com");
    await person.click(screen.getByRole("button", { name: "Discard" }));

    await waitFor(() => expect(bar()).toBeNull());
    expect(name).toHaveValue("Reaper");
    expect(url).toHaveValue("");
    expect(apiMock.saveGeneral).not.toHaveBeenCalled();
  });

  it("holds the whole save while the accent color is half-typed", async () => {
    // The accent is applied app-wide from the stored value, so an unfinished hex code must not
    // be written -- and dropping just that field from a bar that names it would be a lie about
    // what the press did.
    const person = renderPanel();
    const hex = await screen.findByLabelText("Accent color hex code");

    await person.clear(hex);
    await person.type(hex, "#12");

    await waitFor(() => expect(saveChanges()).toBeDisabled());
    expect(bar()!.textContent).toContain("Enter a hex code like #25c3ff to save.");

    await person.type(hex, "3456");
    await waitFor(() => expect(saveChanges()).toBeEnabled());
  });

  it("still re-seeds from the server's canonical value", async () => {
    // Rule 39: the field it sent comes back from the response, trimmed the way the server
    // stored it -- so the row settles on what is really saved, not on what was typed.
    apiMock.saveGeneral.mockResolvedValue({ ...STORED, application_name: "Trimmed" });
    const person = renderPanel();
    const name = await screen.findByLabelText("Application name");

    await person.clear(name);
    await person.type(name, "  Trimmed  ");
    await person.click(saveChanges());

    await waitFor(() => expect(name).toHaveValue("Trimmed"));
  });
});

describe("a control that saves on the spot", () => {
  it("leaves an in-progress edit alone", async () => {
    // B-18, in the shape it survives in: `onSuccess` re-seeded every field from the response, so
    // any save wrote the STORED url back over the one being typed. The per-row Save buttons that
    // first exposed it are gone, but the reverse-proxy switch still writes the moment it is
    // flipped, and it must not take the half-typed URL with it.
    const person = renderPanel();
    const url = await screen.findByLabelText("Application URL");

    await person.type(url, "https://reaper.example.com");
    await person.click(screen.getByLabelText("Behind a reverse proxy"));

    await waitFor(() => expect(apiMock.saveGeneral).toHaveBeenCalledTimes(1));
    expect(apiMock.saveGeneral.mock.calls[0]![0]).toEqual({ proxy_trust_enabled: true });
    // The untouched draft is still exactly as it was left, and still offered by the bar.
    expect(url).toHaveValue("https://reaper.example.com");
    expect(bar()!.textContent).toContain("Application URL");
  });
});
