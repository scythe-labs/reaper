// SPDX-License-Identifier: AGPL-3.0-or-later
// The General panel has one save affordance (rule 43): a bar that names every unsaved field and
// sends them together. What still has to hold is that the controls saving on the spot -- the
// reverse-proxy Switch, the expand-seasons select and the spare-length Segmented -- never throw
// away text being typed elsewhere.
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

// The same row with the two fields that gate themselves switched ON. `STORED` leaves
// `default_spare_days` at 0 and the proxy switch off, which is exactly the state in which
// `spareDirty` and the proxy entry can never be reached -- so against `STORED` alone the bar is
// only ever pinned over two plain strings, and the number and array shapes never travel through
// it at all. Deleting either gate used to leave the whole suite green.
const STORED_BOTH_ON: GeneralSettings = {
  ...STORED,
  default_spare_days: 30,
  proxy_trust_enabled: true,
  trusted_proxies: ["10.0.0.0/8"],
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

  it("carries the number and the list shapes, not just the strings", async () => {
    // The two fields that gate themselves on stored state: `default_spare_days` is only a draft
    // while a length is already in force, and the proxy list only while the switch is on. Both
    // gates are unreachable against the default fixture, so this is the only test that sends a
    // number or an array through the bar -- and the only one that fails if either gate goes.
    apiMock.general.mockResolvedValue(STORED_BOTH_ON);
    const person = renderPanel();
    const days = await screen.findByLabelText("Default spare length in days");
    const proxies = screen.getByLabelText("Trusted proxy addresses");

    await person.clear(days);
    await person.type(days, "90");
    await person.clear(proxies);
    await person.type(proxies, "10.0.0.0/8, 192.168.0.0/16");

    await waitFor(() => expect(bar()!.textContent).toContain("Default spare length"));
    expect(bar()!.textContent).toContain("Trusted proxy addresses");

    await person.click(saveChanges());
    await waitFor(() => expect(apiMock.saveGeneral).toHaveBeenCalledTimes(1));
    expect(apiMock.saveGeneral.mock.calls[0]![0]).toEqual({
      default_spare_days: 90,
      // Split, trimmed and emptied-out, the way the box's text becomes a list.
      trusted_proxies: ["10.0.0.0/8", "192.168.0.0/16"],
    });
  });

  it("says why a refused save was refused, inside the bar", async () => {
    // The route writes all six fields or none, so a refusal costs every draft on the panel. The
    // bar is sticky and the panel is six groups tall, so a notice rendered outside the bar sits
    // at the document foot -- off screen for anyone editing the top group, which is where five
    // of the six fields are. Rule 42: the reason renders where the failed press was.
    apiMock.saveGeneral.mockRejectedValue(new Error("That web address isn't valid."));
    const person = renderPanel();
    const name = await screen.findByLabelText("Application name");

    await person.clear(name);
    await person.type(name, "Second install");
    await person.click(saveChanges());

    await waitFor(() => expect(bar()!.textContent).toContain("That web address isn't valid."));
    // Still unsaved, and still named: nothing was written, and the bar keeps offering it.
    expect(bar()!.textContent).toContain("Application name");
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
