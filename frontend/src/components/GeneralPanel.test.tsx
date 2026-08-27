// SPDX-License-Identifier: AGPL-3.0-or-later
// The General panel has one save affordance: a bar that names every unsaved field and sends
// them together. Two controls still save on the spot, the reverse-proxy switch and the
// expand-seasons select, and neither may discard text the operator is typing elsewhere. The
// spare length is edited by two controls at once, a Forever button and a day box, and both
// stage into the bar rather than writing immediately, so a save always sends what the bar
// shows.
import { act, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { Announcer } from "../announce";
import type { GeneralSettings } from "../api";
import { expectNoA11yViolations } from "../test/a11y";
import { fill } from "../test/forms";
import { testQueryClient } from "../test/queryClient";
import { renderWithProviders } from "../test/renderWithProviders";
import { GeneralPanel } from "./GeneralPanel";

const { apiMock } = await vi.hoisted(async () => ({
  apiMock: (await import("../test/apiMock")).makeApiMock(),
}));

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  api: apiMock,
}));

// `setLanguage` reloads the page, which jsdom cannot do and which would take the test's own
// tree with it. Everything else in the module stays real: the option list and the names on it
// come from `LANGUAGES` and `languageName`, which is what the first test below is about.
const setLanguageMock = vi.hoisted(() => vi.fn());
vi.mock("../i18n", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../i18n")>()),
  setLanguage: setLanguageMock,
}));

const STORED: GeneralSettings = {
  application_name: "Reaper",
  application_url: null,
  timezone: "UTC",
  accent_color: "#38bdf8",
  language: "en",
  api_key_set: false,
  expand_seasons_mode: "off",
  default_spare_days: 0,
  proxy_trust_enabled: false,
  trusted_proxies: [],
  desktop: null,
};

// The same row with both self-gating fields switched on. `STORED` leaves the proxy switch off,
// so the proxy box never appears and the array shape it sends is never exercised there.
// `STORED` also stores Forever, which hides the day box, so the number field is only reachable
// from here.
const STORED_BOTH_ON: GeneralSettings = {
  ...STORED,
  default_spare_days: 30,
  proxy_trust_enabled: true,
  trusted_proxies: ["10.0.0.0/8"],
};

beforeEach(() => {
  vi.clearAllMocks();
  apiMock.general.mockResolvedValue(STORED);
  // The mock answers with the stored row merged with whatever the save sent, the way the real
  // server does: fields the save did not touch keep their old value.
  apiMock.saveGeneral.mockImplementation((body: Partial<GeneralSettings>) =>
    Promise.resolve({ ...STORED, ...body }),
  );
});

function renderPanel() {
  renderWithProviders(
    <>
      {/* Mounted the way the app mounts it: once, above the panel, before anything can speak
          into it. Without it, a call to `announce` writes to a store nothing is listening to,
          so a test could only prove the call did not throw. */}
      <Announcer />
      <GeneralPanel />
    </>,
  );
  return userEvent.setup();
}

/** The same tree, handing back the client, for the one test that needs a refresh the panel
 *  never asked for: a key made in another tab. Nothing the operator does in this render can
 *  produce that state, so the test drives it through the client instead. */
function renderPanelWithClient() {
  const queryClient = testQueryClient();
  renderWithProviders(
    <>
      <Announcer />
      <GeneralPanel />
    </>,
    { client: queryClient },
  );
  return { user: userEvent.setup(), queryClient };
}

/** Text of whichever status region currently holds a sentence, the way a screen reader would read it. */
const announced = () =>
  screen
    .getAllByRole("status")
    .map((r) => r.textContent)
    .filter((t) => t !== "")
    .join("|");

// The same tree plus the draft signal `Settings` subscribes to, for the tests about what this
// panel REPORTS rather than what it renders. The two can be wrong in opposite directions: a
// panel saying it holds nothing loses the draft silently, and one saying it holds something it
// no longer shows asks for a discard the operator cannot act on.
function renderReporting(
  /** Stored row already in the cache, so the panel mounts with `general.data` on its first
   *  render, the way returning to this section does. A fresh client is a cold mount, one render
   *  behind, and a guard can pass there while failing here. */
  cached?: GeneralSettings,
) {
  const onDirtyChange = vi.fn();
  const queryClient = testQueryClient();
  if (cached) queryClient.setQueryData(["general-settings"], cached);
  renderWithProviders(<GeneralPanel onDirtyChange={onDirtyChange} />, { client: queryClient });
  return { person: userEvent.setup(), onDirtyChange, queryClient };
}

const saveChanges = () => screen.getByRole("button", { name: "Save changes" });
const bar = () => document.querySelector(".savebar");

describe("the save bar", () => {
  // Every field on this panel is saved by one bar, so a box the operator cannot identify is a box
  // they type into and then send with the rest. The spare length alone decides how long a file
  // survives a scan.
  it("has no accessibility violations", async () => {
    renderPanel();
    const name = await screen.findByLabelText("Application name");
    await waitFor(() => expect(name).toHaveValue(STORED.application_name));
    await expectNoA11yViolations();
  });

  // `SetRow` is the one component every settings row goes through now, and the class list is
  // the whole of what it decides. Nothing else pins it: the panels' other tests reach controls
  // through their labels, so a `variant` typed wrong renders a row that still reads correct to
  // every one of them and lays out at a different width. All four outcomes live on this panel,
  // which is why the check is here rather than in a file of its own.
  it("gives each row the layout class its stylesheet opt-out asks for", async () => {
    renderPanel();
    await screen.findByLabelText("Application name");
    const rowFor = (label: string) => screen.getByText(label).closest(".set-row");
    expect(rowFor("Application name")).toHaveClass("set-row", { exact: true });
    expect(rowFor("Accent color")).toHaveClass("set-row accent-row", { exact: true });
    expect(rowFor("API key")).toHaveClass("set-row set-row-cluster", { exact: true });
    expect(rowFor("API reference")).toHaveClass("set-row set-row-plain", { exact: true });
    // Off is what `STORED` holds, so the dim arm is the one this render reaches.
    expect(rowFor("Trusted proxy addresses")).toHaveClass("set-row dim", { exact: true });
  });

  it("undims the proxy list once the switch it waits on is on", async () => {
    apiMock.general.mockResolvedValue(STORED_BOTH_ON);
    renderPanel();
    await screen.findByLabelText("Trusted proxy addresses");
    // Both arms, because `dim` is computed on every render rather than set once: a row
    // hardcoded to dim passes the test above and fails only here.
    expect(screen.getByText("Trusted proxy addresses").closest(".set-row")).toHaveClass("set-row", {
      exact: true,
    });
  });

  it("never reports a draft for the frame before the boxes are seeded", async () => {
    // `hasDrafts` is reported up to `Settings` through `onDirtyChange`. For one commit, the
    // panel used to tell the shell it held four unsaved fields before anything had been typed,
    // which is the exact claim a section-switch confirm relies on.
    //
    // This is deterministic even though it is only one frame: the report goes through an
    // effect, so the spy records the call whether or not that frame was ever painted. Asserting
    // on the rendered bar instead could only catch this by luck.
    const { onDirtyChange } = renderReporting();
    await waitFor(() =>
      expect(screen.getByLabelText("Application name")).toHaveValue(STORED.application_name),
    );

    expect(onDirtyChange).not.toHaveBeenCalledWith(true);
  });

  it("never reports a draft when the stored row is already cached", async () => {
    // The warm twin of the test above. The cold test passes for a reason that has nothing to do
    // with the guard: a fresh `QueryClient` leaves `general.data` undefined on the first render,
    // so nothing is compared yet. Returning to this section instead mounts the panel with the
    // row already cached, while the boxes still hold their initial values. `seeded` is
    // `useState(false)`, which no cached value can flip, so the report still holds here. This
    // test is what catches it if `seeded` is ever rewritten as a value derived from the data.
    const { onDirtyChange } = renderReporting(STORED);
    await waitFor(() =>
      expect(screen.getByLabelText("Application name")).toHaveValue(STORED.application_name),
    );

    expect(onDirtyChange).not.toHaveBeenCalledWith(true);
  });

  it("is absent until something is unsaved, and names what is", async () => {
    const person = renderPanel();
    const name = await screen.findByLabelText("Application name");
    // The box is not disabled here, it is unseeded. The form renders on the first data-bearing
    // pass, and an effect after that copies the stored row into local state, so finding the box
    // is not the same as the box holding what the server sent. The wait below is what reaches a
    // seeded box; it is not there because the bar needs time to disappear.
    await waitFor(() => expect(name).toHaveValue(STORED.application_name));
    expect(bar()).toBeNull();

    await fill(person, name, "Second install");

    await waitFor(() => expect(bar()).not.toBeNull());
    expect(bar()!.textContent).toContain("Application name");
    // A field that was never touched is not claimed as unsaved.
    expect(bar()!.textContent).not.toContain("Time zone");
  });

  it("sends every unsaved field in one request", async () => {
    const person = renderPanel();
    const url = await screen.findByLabelText("Application URL");
    const name = screen.getByLabelText("Application name");

    await fill(person, url, "https://reaper.example.com");
    await fill(person, name, "Second install");
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

  it("says the save worked, rather than only taking the bar away", async () => {
    // The savebar unmounting on success also takes the focused button with it, leaving the
    // operator no message and a lost focus point: an absence is not something a screen reader
    // can hear. The sentence is asserted through the live region, so this fails if the
    // `announce` call is dropped from the mutation, or if the region stops being reachable.
    const person = renderPanel();
    const name = await screen.findByLabelText("Application name");

    await fill(person, name, "Second install");
    await person.click(saveChanges());

    await waitFor(() => expect(announced()).toBe("Settings saved."));
    // The bar clears on the server's answer, not on the press, so it is gone by the time the
    // success message is spoken.
    expect(bar()).toBeNull();
  });

  it("puts every draft back on Discard, and sends nothing", async () => {
    const person = renderPanel();
    const name = await screen.findByLabelText("Application name");
    const url = screen.getByLabelText("Application URL");

    await fill(person, name, "Second install");
    await fill(person, url, "https://reaper.example.com");
    await person.click(screen.getByRole("button", { name: "Discard" }));

    await waitFor(() => expect(bar()).toBeNull());
    expect(name).toHaveValue("Reaper");
    expect(url).toHaveValue("");
    expect(apiMock.saveGeneral).not.toHaveBeenCalled();
  });

  it("holds the whole save while the accent color is half-typed", async () => {
    // The accent is applied app-wide from the stored value, so an unfinished hex code must not
    // be written. Dropping just that one field from a bar that names it would misstate what the
    // press actually did.
    const person = renderPanel();
    const hex = await screen.findByLabelText("Accent color hex code");

    await person.clear(hex);
    await person.type(hex, "#12");

    await waitFor(() => expect(saveChanges()).toBeDisabled());
    expect(bar()!.textContent).toContain("Enter a hex code like #25c3ff.");

    await person.type(hex, "3456");
    await waitFor(() => expect(saveChanges()).toBeEnabled());
  });

  it("moves the preview's link with its button, not just the button", async () => {
    // The preview overrides `--accent` and `--accent-ink`, so both the button and the link
    // beside it should follow the typed color. A preview where only the button moves shows two
    // accents at once, on a control whose whole job is to show one.
    //
    // The link's color is `--accent-text`, and it is not derived from `--accent` where it is
    // used: the stylesheet computes it once on `:root` from what `accent.ts` writes there, so
    // overriding `--accent` on a child inherits an ink belonging to a different color. The test
    // asserts that all three move together, rather than against a transcribed hex, so the
    // contrast search stays free to return whatever it returns.
    const person = renderPanel();
    const hex = await screen.findByLabelText("Accent color hex code");
    const preview = document.querySelector(".accent-preview") as HTMLElement;
    expect(preview).toBeTruthy();

    const before = preview.style.getPropertyValue("--accent-text");
    expect(before).not.toBe("");

    await person.clear(hex);
    await person.type(hex, "#7c3aed");

    await waitFor(() => expect(preview.style.getPropertyValue("--accent")).toBe("#7c3aed"));
    expect(preview.style.getPropertyValue("--accent-ink")).not.toBe("");
    expect(preview.style.getPropertyValue("--accent-text")).not.toBe(before);
  });

  it("hands the refused hex box the sentence saying why", async () => {
    // A box that refuses to save must reach the sentence explaining why through
    // `aria-describedby`, not just place the message visibly beside the field. The test checks
    // the accessible description rather than an id string, because that is what a screen reader
    // actually computes: an id pointing at nothing would pass an attribute check while saying
    // nothing.
    const person = renderPanel();
    const hex = await screen.findByLabelText("Accent color hex code");

    await person.clear(hex);
    await person.type(hex, "#12");

    await waitFor(() => expect(hex).toHaveAccessibleDescription("Enter a hex code like #25c3ff."));
    expect(hex).toHaveAttribute("aria-invalid", "true");

    // And it lets go once the value is usable: a box still marked invalid over a value that
    // saves is the same lie in the other direction.
    await person.type(hex, "3456");

    await waitFor(() => expect(hex).not.toHaveAttribute("aria-invalid"));
    expect(hex).toHaveAccessibleDescription("");
  });

  it("still re-seeds from the server's canonical value", async () => {
    // The field it sent comes back from the response, trimmed the way the server stores it, so
    // the row settles on what is really saved rather than on what was typed.
    apiMock.saveGeneral.mockResolvedValue({ ...STORED, application_name: "Trimmed" });
    const person = renderPanel();
    const name = await screen.findByLabelText("Application name");

    await fill(person, name, "  Trimmed  ");
    await person.click(saveChanges());

    await waitFor(() => expect(name).toHaveValue("Trimmed"));
  });

  it("carries the number and the list shapes, not just the strings", async () => {
    // The two shapes that are not plain strings. The day box only exists while the draft is a
    // length, and the proxy list only joins the bar while the switch is on. That gate is
    // unreachable against the default fixture, so this is the only test that sends an array
    // through the bar and the only one that fails if the gate breaks.
    apiMock.general.mockResolvedValue(STORED_BOTH_ON);
    const person = renderPanel();
    const days = await screen.findByLabelText("Default spare length in days");
    const proxies = screen.getByLabelText("Trusted proxy addresses");

    await person.clear(days);
    await person.type(days, "90");
    await fill(person, proxies, "10.0.0.0/8, 192.168.0.0/16");

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
    // at the document foot, off screen for anyone editing the top group, where five of the six
    // fields live. The reason must render where the failed press happened.
    apiMock.saveGeneral.mockRejectedValue(new Error("That web address isn't valid."));
    const person = renderPanel();
    const name = await screen.findByLabelText("Application name");

    await fill(person, name, "Second install");
    await person.click(saveChanges());

    await waitFor(() => expect(bar()!.textContent).toContain("That web address isn't valid."));
    // Still unsaved, and still named: nothing was written, and the bar keeps offering it.
    expect(bar()!.textContent).toContain("Application name");
  });
});

describe("the default spare length", () => {
  // One stored field, two controls. Forever is zero in that field, so the mode press and the
  // typed number are both part of one draft, and both belong in the bar.
  const forever = () => screen.getByRole("button", { name: "Forever" });
  const days = () => screen.getByRole("button", { name: "Days" });
  const dayBox = () => screen.queryByLabelText("Default spare length in days");

  it("stages a Forever press instead of writing it, and keeps the Discard", async () => {
    // From a stored 365 the operator types 7. The bar names the field and offers Discard.
    // Pressing Forever must stage the change rather than write it on the spot: writing 0
    // immediately would drop the field out of the pending set and unmount the bar, taking the
    // Discard with it while the box still showed the 7, so the next press would send an
    // unsaved 7.
    apiMock.general.mockResolvedValue({ ...STORED_BOTH_ON, default_spare_days: 365 });
    const person = renderPanel();
    const box = await screen.findByLabelText("Default spare length in days");

    await person.clear(box);
    await person.type(box, "7");
    await waitFor(() => expect(bar()!.textContent).toContain("Default spare length"));

    await person.click(forever());

    expect(apiMock.saveGeneral).not.toHaveBeenCalled();
    // Still one unsaved field, still undoable. The box is gone because Forever has no length to
    // show.
    expect(bar()!.textContent).toContain("Default spare length");
    expect(screen.getByRole("button", { name: "Discard" })).toBeInTheDocument();
    expect(dayBox()).toBeNull();

    // Coming back is just as free: pressing Days again sends nothing.
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
    // Re-seeded from the response, so the mode settles on what is really stored.
    await waitFor(() => expect(bar()).toBeNull());
    expect(dayBox()).toBeNull();
  });

  it("stages a first length from Forever, and shows the number being agreed to", async () => {
    // From a stored Forever there is no box, so the press that reveals one is also the press
    // that stages it. It must not save that number before the operator has seen it.
    const person = renderPanel();
    await screen.findByLabelText("Application name");
    // STORED is Forever, but the length box is visible for the first paint, and the effect that
    // reads default_spare_days: 0 back into Forever removes it only after that commit. Landing
    // on Application name does not gate that effect, so the box's absence is asserted through
    // waitFor rather than synchronously; a synchronous read can land in the pre-seed window and
    // see the box carrying 30.
    await waitFor(() => expect(dayBox()).toBeNull());

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
    // number. `STORED` is Forever, where the box exists only after a press of Days. Discard
    // must clear the number there too, not just leave the discarded figure sitting in the
    // hidden box for the next press to re-stage. Nothing is written either way, so what would
    // be lost is Discard meaning "all of it".
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

  it("stops taking presses while the save carrying it is in flight", async () => {
    // `Segmented` needs `disabled={save.isPending}` like every neighboring control in the row,
    // or this is the one control still pressable during a save: a press flips `aria-pressed` to
    // read as taken, then `onSuccess` re-seeds the mode from the response and clears the bar in
    // the same flush, leaving nothing on screen to say the press was dropped.
    //
    // What this test pins is the gap during the save, not the end state: after the response the
    // mode reads Days either way, because the re-seed overwrites the press. The in-flight moment
    // is the only place a refused press and a silently dropped one look different.
    apiMock.general.mockResolvedValue({ ...STORED_BOTH_ON, default_spare_days: 365 });
    const person = renderPanel();
    const box = await screen.findByLabelText("Default spare length in days");

    await person.clear(box);
    await person.type(box, "7");
    await waitFor(() => expect(bar()!.textContent).toContain("Default spare length"));

    // Hold the save open, so the press lands in the window the response has not closed yet.
    let release: (row: GeneralSettings) => void = () => {};
    apiMock.saveGeneral.mockImplementation(
      () =>
        new Promise<GeneralSettings>((resolve) => {
          release = resolve;
        }),
    );
    await person.click(saveChanges());

    // Both segments are checked, not only the one being pressed: the same drop would happen to
    // a Days press during a save that writes 0.
    await waitFor(() => expect(forever()).toBeDisabled());
    expect(days()).toBeDisabled();

    // user-event dispatches nothing at a disabled target and reports success, which matches the
    // behavior under test: the pair must not move here.
    await person.click(forever());
    expect(forever()).toHaveAttribute("aria-pressed", "false");
    expect(days()).toHaveAttribute("aria-pressed", "true");

    await act(async () => {
      release({ ...STORED_BOTH_ON, default_spare_days: 7 });
    });

    // One request, carrying the number that was actually staged, and the row settles on it.
    await waitFor(() => expect(bar()).toBeNull());
    expect(apiMock.saveGeneral).toHaveBeenCalledTimes(1);
    expect(apiMock.saveGeneral.mock.calls[0]![0]).toEqual({ default_spare_days: 7 });
    expect(dayBox()).toHaveValue(7);
  });
});

describe("what the panel reports to the section rail", () => {
  it("counts a proxy list parked behind its own switch", async () => {
    // The bar drops this field on purpose, since it must not name a box the operator cannot
    // reach to fix. But the typed text is still sitting in the disabled box, still unsaved, and
    // still lost on unmount, so the dirty report to the section rail must count it even though
    // the bar does not show it.
    apiMock.general.mockResolvedValue(STORED_BOTH_ON);
    apiMock.saveGeneral.mockImplementation((body: Partial<GeneralSettings>) =>
      Promise.resolve({ ...STORED_BOTH_ON, ...body }),
    );
    const { person, onDirtyChange } = renderReporting();
    const box = await screen.findByLabelText("Trusted proxy addresses");

    await fill(person, box, "10.9.0.0/16");
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

    await fill(person, name, "Second install");
    await waitFor(() => expect(onDirtyChange).toHaveBeenLastCalledWith(true));

    // Generate API key and Remove key both invalidate this same query, so a server that blinks
    // can land here with a draft already on screen. React Query keeps the last good row and
    // raises `isError` beside it, so the panel must keep showing the form: replacing it with one
    // paragraph would report something unsaved to lose while giving no way to see or keep it.
    apiMock.general.mockRejectedValue(new Error("boom"));
    await queryClient.invalidateQueries({ queryKey: ["general-settings"] });
    await waitFor(() => expect(apiMock.general).toHaveBeenCalledTimes(2));

    expect(screen.queryByText(/Couldn't load these settings/)).toBeNull();
    expect(screen.getByLabelText("Application name")).toHaveValue("Second install");
    expect(bar()!.textContent).toContain("Application name");
    expect(screen.getByRole("button", { name: "Discard" })).toBeInTheDocument();
    expect(onDirtyChange).toHaveBeenLastCalledWith(true);

    // And it says the read failed. Keeping the form is what keeps the draft reachable; keeping
    // it with nothing said would present values the panel knows are stale as if they were
    // current. Same line as `PlexPanel`, from one shared component, so the two cannot drift
    // apart.
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

describe("the display language picker", () => {
  it("names each shipped language in that language, and offers no browser-match entry", async () => {
    renderPanel();
    const select = await screen.findByLabelText<HTMLSelectElement>("Language");

    // "Español", not "Spanish": an operator scanning for their language looks for the name they
    // call it themselves. The list comes from the same glob the loader reads, so a translation
    // that ships is choosable with no edit here, which is also why this asserts on the two
    // languages that ship rather than on the whole list.
    expect(select.textContent).toContain("English");
    expect(select.textContent).toContain("Espa\u00f1ol");
    expect(select.textContent).not.toContain("Spanish");

    // The browser still decides on a fresh install, through `useSeedLanguage`, but as a seed
    // written to the server rather than a standing mode. Leaving the entry here would be the
    // one choice under which a notification is written in a different language from the app,
    // which is the split this control exists to close.
    expect(select.textContent).not.toContain("Match my browser");
    expect(select.value).toBe("en");
  });

  it("saves the pick to the server before repainting this browser", async () => {
    const person = renderPanel();
    const select = await screen.findByLabelText<HTMLSelectElement>("Language");
    apiMock.saveGeneral.mockResolvedValue({ ...STORED, language: "es" });

    await person.selectOptions(select, "es");

    // Order is the assertion, not just the pair. The server holds what a notification is
    // written in, so a refused save must leave this browser on the old language too rather
    // than painting one language over a server storing another.
    await waitFor(() => expect(apiMock.saveGeneral).toHaveBeenCalledTimes(1));
    expect(apiMock.saveGeneral.mock.calls[0]![0]).toEqual({ language: "es" });
    await waitFor(() => expect(setLanguageMock).toHaveBeenCalledWith("es"));
  });

  it("does not repaint when the save is refused", async () => {
    const person = renderPanel();
    const select = await screen.findByLabelText<HTMLSelectElement>("Language");
    apiMock.saveGeneral.mockRejectedValue(new Error("nope"));

    await person.selectOptions(select, "es");

    await waitFor(() => expect(apiMock.saveGeneral).toHaveBeenCalledTimes(1));
    expect(apiMock.saveGeneral.mock.calls[0]![0]).toEqual({ language: "es" });
    expect(setLanguageMock).not.toHaveBeenCalled();
  });

  it("cannot be reached while the save bar is holding a draft", async () => {
    const person = renderPanel();
    const name = await screen.findByLabelText("Application name");
    await waitFor(() => expect(name).toHaveValue(STORED.application_name));
    const select = screen.getByLabelText("Language");
    expect(select).toBeEnabled();

    await fill(person, name, "Second install");

    // Picking a language reloads the page (`setLanguage`), and this panel's bar is the one draft
    // that can be on screen when it fires, since Settings shows one panel at a time. The gate in
    // `test_repo_hygiene.py` that pins every reload in the tree rests on exactly this.
    await waitFor(() => expect(bar()).not.toBeNull());
    expect(select).toBeDisabled();
  });
});

// Generating replaces whatever key the server holds, immediately and with no undo, which is why
// Replace is a two-step confirm. A one-click Generate rendered on a cached `api_key_set: false`
// (30-second staleTime, no refetch on focus, nothing evicting it) would let a key made from
// another tab, a phone, or another admin sit behind a one-click revoke with no confirmation.
//
// The button re-checks for a key before acting, instead of trusting the cache, so all three
// answers to "is there a key" are pinned here. Only the no-key answer generates on one press;
// the other two are the states where the page cannot yet show that nothing is about to be
// destroyed.
describe("the Generate API key button", () => {
  const generate = () => screen.findByRole("button", { name: "Generate API key" });

  beforeEach(() => {
    apiMock.generateApiKey.mockResolvedValue({ key: "generated-key" });
  });

  it("generates on one press when it has just checked and there is no key", async () => {
    // The ordinary first run. A danger confirm here would be its own false claim: nothing is
    // being replaced, and the page has just confirmed that.
    const user = renderPanel();
    await user.click(await generate());

    await waitFor(() => expect(apiMock.generateApiKey).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole("button", { name: "Confirm generate" })).toBeNull();
  });

  it("asks first when it cannot check, then generates on the confirmed press", async () => {
    const user = renderPanel();
    const button = await generate();

    // The re-read fails, so nothing here can prove there is no key to destroy.
    apiMock.general.mockRejectedValue(new Error("boom"));
    await user.click(button);

    const confirm = await screen.findByRole("button", { name: "Confirm generate" });
    expect(apiMock.generateApiKey).not.toHaveBeenCalled();
    expect(screen.getByText(/Couldn't check for an existing key/)).toBeInTheDocument();

    // The second press is the operator saying they accept replacing one they cannot see.
    await user.click(confirm);
    await waitFor(() => expect(apiMock.generateApiKey).toHaveBeenCalledTimes(1));
  });

  it("refuses and offers Replace when the re-read turns up a key the page did not know about", async () => {
    const user = renderPanel();
    const button = await generate();

    // Another admin, another tab, a phone: any of these can have added a key since the last
    // read. This press must not revoke it.
    apiMock.general.mockResolvedValue({ ...STORED, api_key_set: true });
    await user.click(button);

    await waitFor(() => expect(screen.getByText(/A key already exists/)).toBeInTheDocument());
    expect(apiMock.generateApiKey).not.toHaveBeenCalled();
    // And the row is now the key-present one, so the two-step Replace is what is on offer.
    expect(screen.getByRole("button", { name: "Replace…" })).toBeInTheDocument();
  });

  // One `confirmReplace` flag arms a danger button on both rows, and `api_key_set` decides
  // which row renders, so the flag crossing between them can arm a confirm nobody opened. Both
  // directions are driven here because both are reachable, and they fail differently: one shows
  // a danger button with no notice, the other leaves a live key one press from revocation.
  it("does not leave a generate confirm armed after the key is removed", async () => {
    apiMock.general.mockResolvedValue({ ...STORED, api_key_set: true });
    const user = renderPanel();

    await user.click(await screen.findByRole("button", { name: "Replace…" }));
    expect(await screen.findByRole("button", { name: "Confirm replace" })).toBeInTheDocument();

    // Changed their mind: remove it instead. Remove and Replace render side by side, so no
    // backing out is needed to reach it.
    apiMock.removeApiKey.mockImplementation(async () => {
      apiMock.general.mockResolvedValue({ ...STORED, api_key_set: false });
    });
    await user.click(screen.getByRole("button", { name: "Remove…" }));
    await user.click(screen.getByRole("button", { name: "Confirm remove" }));

    // The no-key row must rest on its plain one-click Generate, not open already armed on a red
    // "Confirm generate" asking the operator to confirm destroying a key the page has just
    // proved is gone.
    expect(await screen.findByRole("button", { name: "Generate API key" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Confirm generate" })).toBeNull();
  });

  it("does not leave a replace confirm armed when a key arrives from elsewhere", async () => {
    const { user, queryClient } = renderPanelWithClient();
    const button = await screen.findByRole("button", { name: "Generate API key" });

    // The re-read fails, so the panel falls back to asking.
    apiMock.general.mockRejectedValue(new Error("boom"));
    await user.click(button);
    expect(await screen.findByRole("button", { name: "Confirm generate" })).toBeInTheDocument();

    // Now a key made in another tab lands, the same scenario as before, pointed the other way.
    apiMock.general.mockResolvedValue({ ...STORED, api_key_set: true });
    await act(async () => {
      await queryClient.invalidateQueries({ queryKey: ["general-settings"] });
    });

    // The key-present row must rest on Replace…, not render with "Confirm replace" already
    // armed, which would let one press revoke a live key through a confirm the operator never
    // opened.
    expect(await screen.findByRole("button", { name: "Replace…" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Confirm replace" })).toBeNull();
  });

  it("takes the notice away with the confirm when you back out", async () => {
    const user = renderPanel();
    const button = await screen.findByRole("button", { name: "Generate API key" });

    apiMock.general.mockRejectedValue(new Error("boom"));
    await user.click(button);
    expect(screen.getByText(/Couldn't check for an existing key/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Cancel" }));

    // The sentence explains a confirm, so it must go when the confirm does; leaving it behind
    // would tell the operator that confirming replaces a key with no confirm anywhere on the
    // page.
    expect(screen.queryByText(/Couldn't check for an existing key/)).toBeNull();
  });

  it("says it is checking, and cannot be pressed again while it does", async () => {
    const user = renderPanel();
    const button = await screen.findByRole("button", { name: "Generate API key" });

    // Hold the re-read open: in this window the button must not sit enabled under its idle
    // label, since `generate` has not started yet and the only pending flag it can read is
    // false.
    let release: (row: GeneralSettings) => void = () => {};
    apiMock.general.mockImplementation(
      () =>
        new Promise<GeneralSettings>((resolve) => {
          release = resolve;
        }),
    );
    await user.click(button);

    const checking = await screen.findByRole("button", { name: "Checking…" });
    expect(checking).toBeDisabled();

    // The second press is what minted a second key: two checks, both clearing, two POSTs, and the
    // box left holding whichever response came back last.
    await user.click(checking);
    release({ ...STORED, api_key_set: false });

    await waitFor(() => expect(apiMock.generateApiKey).toHaveBeenCalledTimes(1));
  });
});

describe("a control that saves on the spot", () => {
  it("leaves an in-progress edit alone", async () => {
    // `onSuccess` must not re-seed every field from the response: doing that would write the
    // stored URL back over one still being typed. The reverse-proxy switch writes the moment it
    // is flipped, and it must not take a half-typed URL field with it.
    const person = renderPanel();
    const url = await screen.findByLabelText("Application URL");

    await fill(person, url, "https://reaper.example.com");
    await person.click(screen.getByLabelText("Behind a reverse proxy"));

    await waitFor(() => expect(apiMock.saveGeneral).toHaveBeenCalledTimes(1));
    expect(apiMock.saveGeneral.mock.calls[0]![0]).toEqual({ proxy_trust_enabled: true });
    // The untouched draft is still exactly as it was left, and still offered by the bar.
    expect(url).toHaveValue("https://reaper.example.com");
    expect(bar()!.textContent).toContain("Application URL");
  });
});

describe("the Desktop app group", () => {
  // The three install shapes the server can report. Only the two desktop ones render the
  // group, and the rows differ by platform; every other install is `desktop: null`, which
  // `STORED` already holds, so the absence case rides the default fixture.
  const MACOS: GeneralSettings = {
    ...STORED,
    desktop: { platform: "macos", tray: true, dock_icon: false },
  };
  const WINDOWS: GeneralSettings = {
    ...STORED,
    desktop: { platform: "windows", tray: true, dock_icon: false },
  };

  /** Merge a tray/dock save back into the desktop object the way the server does, so the
   *  switch's post-save state renders from the response rather than a stray top-level key. */
  const foldDesktop = (stored: GeneralSettings) =>
    apiMock.saveGeneral.mockImplementation((body: { tray?: boolean; dock_icon?: boolean }) =>
      Promise.resolve({
        ...stored,
        desktop: {
          ...stored.desktop!,
          ...(body.tray !== undefined ? { tray: body.tray } : {}),
          ...(body.dock_icon !== undefined ? { dock_icon: body.dock_icon } : {}),
        },
      }),
    );

  it("never renders off the desktop builds", async () => {
    renderPanel();
    await screen.findByLabelText("Behind a reverse proxy");
    expect(screen.queryByText("Desktop app")).toBeNull();
    expect(screen.queryByLabelText("Tray icon")).toBeNull();
    expect(screen.queryByLabelText("Menu bar icon")).toBeNull();
  });

  it("offers the Mac rows, and the Dock icon saves on the spot", async () => {
    apiMock.general.mockResolvedValue(MACOS);
    foldDesktop(MACOS);
    const person = renderPanel();
    const dock = await screen.findByLabelText("Show the Dock icon");
    expect(screen.getByLabelText("Menu bar icon")).toBeChecked();
    expect(dock).not.toBeChecked();

    await person.click(dock);
    await waitFor(() => expect(apiMock.saveGeneral).toHaveBeenCalled());
    expect(apiMock.saveGeneral.mock.calls[0]![0]).toEqual({ dock_icon: true });
    // On the spot means no bar: nothing is left unsaved by this press.
    expect(bar()).toBeNull();
    await waitFor(() => expect(screen.getByLabelText("Show the Dock icon")).toBeChecked());
  });

  it("offers only the tray on Windows", async () => {
    apiMock.general.mockResolvedValue(WINDOWS);
    foldDesktop(WINDOWS);
    const person = renderPanel();
    const tray = await screen.findByLabelText("Tray icon");
    expect(screen.queryByLabelText("Show the Dock icon")).toBeNull();
    expect(screen.queryByLabelText("Menu bar icon")).toBeNull();

    await person.click(tray);
    await waitFor(() => expect(apiMock.saveGeneral).toHaveBeenCalled());
    expect(apiMock.saveGeneral.mock.calls[0]![0]).toEqual({ tray: false });
    await waitFor(() => expect(screen.getByLabelText("Tray icon")).not.toBeChecked());
  });

  it("has no accessibility violations with the group shown", async () => {
    apiMock.general.mockResolvedValue(MACOS);
    const { container } = renderWithProviders(
      <>
        <Announcer />
        <GeneralPanel />
      </>,
    );
    await screen.findByLabelText("Show the Dock icon");
    await expectNoA11yViolations(container);
  });
});
