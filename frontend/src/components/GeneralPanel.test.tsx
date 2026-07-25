// SPDX-License-Identifier: AGPL-3.0-or-later
// Every row on the General panel has its own Save button, so a save has to be scoped to its own
// row: pressing one must never quietly throw away what is being typed in another.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { GeneralSettings } from "../api";
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
  expand_seasons_default: false,
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
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <GeneralPanel />
    </QueryClientProvider>,
  );
  return userEvent.setup();
}

/** The Save button belonging to one row, found through that row's own labeled control. */
const saveFor = (label: string) =>
  within(screen.getByLabelText(label).closest(".set-row")!).getByRole("button", { name: "Save" });

describe("saving one row", () => {
  it("leaves another row's in-progress edit alone", async () => {
    // B-18: `onSuccess` re-seeded every field from the response, so saving the name row wrote
    // the STORED url back over the one being typed. The typed value and its own Save button
    // both vanished, with nothing on screen to say why.
    const person = renderPanel();
    const url = await screen.findByLabelText("Application URL");

    await person.type(url, "https://reaper.example.com");
    const name = screen.getByLabelText("Application name");
    await person.clear(name);
    await person.type(name, "Second install");
    await person.click(saveFor("Application name"));

    await waitFor(() => expect(apiMock.saveGeneral).toHaveBeenCalledTimes(1));
    // The body only, not React Query's trailing context argument.
    expect(apiMock.saveGeneral.mock.calls[0]![0]).toEqual({ application_name: "Second install" });
    // The untouched row is still exactly as it was left, Save button and all.
    expect(url).toHaveValue("https://reaper.example.com");
    expect(saveFor("Application URL")).toBeInTheDocument();
  });

  it("still re-seeds the row it did save, from the server's canonical value", async () => {
    // Rule 39: the field it sent comes back from the response, trimmed the way the server
    // stored it -- so the row settles on what is really saved, not on what was typed.
    apiMock.saveGeneral.mockResolvedValue({ ...STORED, application_name: "Trimmed" });
    const person = renderPanel();
    const name = await screen.findByLabelText("Application name");

    await person.clear(name);
    await person.type(name, "  Trimmed  ");
    await person.click(saveFor("Application name"));

    await waitFor(() => expect(name).toHaveValue("Trimmed"));
  });
});
