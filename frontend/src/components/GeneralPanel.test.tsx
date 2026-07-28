// SPDX-License-Identifier: AGPL-3.0-or-later
// The General panel has one save affordance (rule 43): a bar that names every unsaved field and
// sends them together. Two things have to hold. The controls that still save on the spot -- the
// reverse-proxy Switch and the expand-seasons select -- never throw away text being typed
// elsewhere. And the spare length, which is the one field edited by two controls at once, stages
// BOTH of them in the bar: it was a third on-the-spot writer, and pressing Forever committed the
// number the bar had just called unsaved (issue #90).
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

// The same row with the two self-gating fields switched ON. `STORED` leaves the proxy switch
// off, which is exactly the state in which the proxy entry can never be reached -- so against
// `STORED` alone the array shape never travels through the bar. It also stores Forever, which
// hides the day box, so the number is only typeable from here. Deleting the proxy gate used to
// leave the whole suite green.
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

// The same tree plus the draft signal `Settings` subscribes to, for the tests about what this
// panel REPORTS rather than what it renders. The two can be wrong in opposite directions: a
// panel saying it holds nothing loses the draft silently, and one saying it holds something it
// no longer shows asks for a discard the operator cannot act on.
function renderReporting() {
  const onDirtyChange = vi.fn();
  const queryClient = testQueryClient();
  render(
    <QueryClientProvider client={queryClient}>
      <GeneralPanel onDirtyChange={onDirtyChange} />
    </QueryClientProvider>,
  );
  return { person: userEvent.setup(), onDirtyChange, queryClient };
}

const saveChanges = () => screen.getByRole("button", { name: "Save changes" });
const bar = () => document.querySelector(".savebar");

describe("the save bar", () => {
  it("is absent until something is unsaved, and names what is", async () => {
    const person = renderPanel();
    const name = await screen.findByLabelText("Application name");
    // Rule 137, one turn earlier than usual: the box is not disabled here, it is UNSEEDED. The
    // form renders on the first data-bearing pass and the stored row is copied into local state
    // by an effect after it, so for that one flush every field still holds its initial value,
    // differs from `data`, and the bar is correctly on screen naming four of them. Finding the
    // box is not the same as the box holding what the server sent, so wait for the seed rather
    // than for the markup -- this asserted straight after the find and failed about one run in
    // three under load, reading as a bar that appears with nothing typed.
    await waitFor(() => expect(name).toHaveValue(STORED.application_name));
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
    // The two shapes that are not plain strings. The day box only exists while the draft is a
    // length, and the proxy list only joins the bar while the switch is on -- a gate unreachable
    // against the default fixture, so this is the only test that sends an array through the bar
    // and the only one that fails if that gate goes.
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

describe("the default spare length", () => {
  // One stored field, two controls. Forever IS zero in that field, so the mode press and the
  // typed number are halves of one draft and both belong in the bar.
  const forever = () => screen.getByRole("button", { name: "Forever" });
  const days = () => screen.getByRole("button", { name: "Days" });
  const dayBox = () => screen.queryByLabelText("Default spare length in days");

  it("stages a Forever press instead of writing it, and keeps the Discard", async () => {
    // Issue #90, in the order it was driven. From a stored 365 the operator types 7; the bar
    // names the field and offers Discard. Pressing Forever used to write 0 on the spot, which
    // dropped the field out of `pending` and unmounted the bar -- taking that Discard with it
    // while the box kept the 7. The next press then sent the 7 nobody had saved.
    apiMock.general.mockResolvedValue({ ...STORED_BOTH_ON, default_spare_days: 365 });
    const person = renderPanel();
    const box = await screen.findByLabelText("Default spare length in days");

    await person.clear(box);
    await person.type(box, "7");
    await waitFor(() => expect(bar()!.textContent).toContain("Default spare length"));

    await person.click(forever());

    expect(apiMock.saveGeneral).not.toHaveBeenCalled();
    // Still one unsaved field, still undoable, and the box is gone because Forever has no
    // length to show -- not because the field left the bar.
    expect(bar()!.textContent).toContain("Default spare length");
    expect(screen.getByRole("button", { name: "Discard" })).toBeInTheDocument();
    expect(dayBox()).toBeNull();

    // Coming back is just as free: the press that used to commit the 7 sends nothing.
    await person.click(days());
    expect(apiMock.saveGeneral).not.toHaveBeenCalled();
    expect(dayBox()).toHaveValue(7);
  });

  it("sends Forever as a length of zero, once Save is pressed", async () => {
    apiMock.general.mockResolvedValue(STORED_BOTH_ON);
    apiMock.saveGeneral.mockImplementation((body: Partial<GeneralSettings>) =>
      Promise.resolve({ ...STORED_BOTH_ON, ...body }),
    );
    const person = renderPanel();
    await screen.findByLabelText("Default spare length in days");

    await person.click(forever());
    await waitFor(() => expect(bar()!.textContent).toContain("Default spare length"));
    await person.click(saveChanges());

    await waitFor(() => expect(apiMock.saveGeneral).toHaveBeenCalledTimes(1));
    expect(apiMock.saveGeneral.mock.calls[0]![0]).toEqual({ default_spare_days: 0 });
    // Re-seeded from the response, so the mode settles on what is really stored (rule 39).
    await waitFor(() => expect(bar()).toBeNull());
    expect(dayBox()).toBeNull();
  });

  it("stages a first length from Forever, and shows the number being agreed to", async () => {
    // From a stored Forever there is no box, so the press that reveals one is also the press
    // that stages it. It used to store 30 before the operator had seen that number at all.
    const person = renderPanel();
    await screen.findByLabelText("Application name");
    expect(dayBox()).toBeNull();

    await person.click(days());

    expect(apiMock.saveGeneral).not.toHaveBeenCalled();
    expect(dayBox()).toHaveValue(30);
    await waitFor(() => expect(bar()!.textContent).toContain("Default spare length"));
  });

  it("puts the mode back on Discard, not just the number", async () => {
    apiMock.general.mockResolvedValue(STORED_BOTH_ON);
    const person = renderPanel();
    const box = await screen.findByLabelText("Default spare length in days");

    await person.clear(box);
    await person.type(box, "7");
    await person.click(forever());
    await person.click(screen.getByRole("button", { name: "Discard" }));

    await waitFor(() => expect(bar()).toBeNull());
    expect(dayBox()).toHaveValue(30);
    expect(apiMock.saveGeneral).not.toHaveBeenCalled();
  });

  it("puts the number back on Discard even when Forever is what is stored", async () => {
    // The test above runs against a stored 30, so it never reaches the branch that skips the
    // number. `STORED` is Forever, where the box exists only after a press of Days -- and there
    // Discard left the discarded figure sitting in the hidden box, for the next press to
    // re-stage. Nothing is written either way, so what is lost is Discard meaning "all of it".
    const person = renderPanel();
    await screen.findByLabelText("Application name");

    await person.click(days());
    const box = screen.getByLabelText("Default spare length in days");
    await person.clear(box);
    await person.type(box, "365");
    await waitFor(() => expect(bar()!.textContent).toContain("Default spare length"));

    await person.click(screen.getByRole("button", { name: "Discard" }));
    await waitFor(() => expect(bar()).toBeNull());
    expect(dayBox()).toBeNull();

    // Pressing Days again opens on the seed, not on the number that was just discarded.
    await person.click(days());
    expect(dayBox()).toHaveValue(30);
    expect(apiMock.saveGeneral).not.toHaveBeenCalled();
  });
});

describe("what the panel reports to the section rail", () => {
  it("counts a proxy list parked behind its own switch", async () => {
    // The bar drops that field on purpose (it must not name a box the operator cannot reach to
    // fix), but the text is still in the disabled box, still unsaved, and still gone on unmount.
    // Reading the bar alone let exactly that one walk out with no confirm, on the panel that had
    // just promised to ask.
    apiMock.general.mockResolvedValue(STORED_BOTH_ON);
    apiMock.saveGeneral.mockImplementation((body: Partial<GeneralSettings>) =>
      Promise.resolve({ ...STORED_BOTH_ON, ...body }),
    );
    const { person, onDirtyChange } = renderReporting();
    const box = await screen.findByLabelText("Trusted proxy addresses");

    await person.clear(box);
    await person.type(box, "10.9.0.0/16");
    await waitFor(() => expect(onDirtyChange).toHaveBeenLastCalledWith(true));

    await person.click(screen.getByLabelText("Behind a reverse proxy"));
    await waitFor(() => expect(apiMock.saveGeneral).toHaveBeenCalledTimes(1));

    // The bar is right to go quiet. The panel is not right to say it holds nothing.
    await waitFor(() => expect(bar()).toBeNull());
    expect(box).toBeDisabled();
    expect(box).toHaveValue("10.9.0.0/16");
    expect(onDirtyChange).toHaveBeenLastCalledWith(true);
  });

  it("keeps the form when a refetch fails, so the draft it reports stays reachable", async () => {
    const { person, onDirtyChange, queryClient } = renderReporting();
    const name = await screen.findByLabelText("Application name");

    await person.clear(name);
    await person.type(name, "Second install");
    await waitFor(() => expect(onDirtyChange).toHaveBeenLastCalledWith(true));

    // Generate API key and Remove key both invalidate this very query, so a server that blinks
    // lands here with a draft on screen. React Query keeps the last good row and raises
    // `isError` beside it, and the panel used to trade the whole form for one paragraph: no
    // fields, no bar, no Discard, while still reporting something unsaved to lose.
    apiMock.general.mockRejectedValue(new Error("boom"));
    await queryClient.invalidateQueries({ queryKey: ["general-settings"] });
    await waitFor(() => expect(apiMock.general).toHaveBeenCalledTimes(2));

    expect(screen.queryByText(/Couldn't load these settings/)).toBeNull();
    expect(screen.getByLabelText("Application name")).toHaveValue("Second install");
    expect(bar()!.textContent).toContain("Application name");
    expect(screen.getByRole("button", { name: "Discard" })).toBeInTheDocument();
    expect(onDirtyChange).toHaveBeenLastCalledWith(true);

    // And it says the read failed. Keeping the form is what keeps the draft reachable; keeping
    // it with nothing said presents values the panel knows are stale as current (rule 17/36).
    // Same line as `PlexPanel`, from one component, so the two cannot drift (rules 72 and 144).
    const stale = await screen.findByText(/Couldn't check these settings just now/);
    expect(stale).toHaveClass("notice-warn");
  });

  it("still says so when the read that fails is the first one", async () => {
    apiMock.general.mockRejectedValue(new Error("boom"));
    renderReporting();

    expect(await screen.findByText(/Couldn't load these settings/)).toBeInTheDocument();
    expect(bar()).toBeNull();
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
