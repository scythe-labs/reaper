// SPDX-License-Identifier: AGPL-3.0-or-later
// The General panel has one save affordance (rule 43): a bar that names every unsaved field and
// sends them together. Two things have to hold. The controls that still save on the spot -- the
// reverse-proxy Switch and the expand-seasons select -- never throw away text being typed
// elsewhere. And the spare length, which is the one field edited by two controls at once, stages
// BOTH of them in the bar: it was a third on-the-spot writer, and pressing Forever committed the
// number the bar had just called unsaved (issue #90).
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
  api_key_set: false,
  expand_seasons_mode: "off",
  default_spare_days: 0,
  proxy_trust_enabled: false,
  trusted_proxies: [],
  desktop: null,
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
  renderWithProviders(
    <>
      {/* Mounted the way the app mounts it -- once, above the panel, before anything can speak
          into it. Without it in the tree a call to `announce` writes to a store nothing is
          listening to, so a test could only ever prove the call did not throw. */}
      <Announcer />
      <GeneralPanel />
    </>,
  );
  return userEvent.setup();
}

/** The same tree, handing back the client, for the one test that needs a refresh the panel never
 *  asked for. A key made in another tab is exactly that: nothing the operator does here can
 *  produce it, and it is the state #203 is about. */
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

/** What a screen reader would have heard: the region currently holding a sentence. */
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
  /** Stored row already in the cache, so the panel mounts with `general.data` on its FIRST
   *  render -- what returning to this section does. A fresh client is a COLD mount, one render
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
    // Both arms, because `dim` is the one class computed per render rather than declared once
    // (rule 145): a row hardcoded to dim passes the test above and fails only here.
    expect(screen.getByText("Trusted proxy addresses").closest(".set-row")).toHaveClass("set-row", {
      exact: true,
    });
  });

  it("never reports a draft for the frame before the boxes are seeded", async () => {
    // #139. The bar flashing was the visible half; this is the half that mattered. `hasDrafts`
    // is reported up to `Settings` through `onDirtyChange`, so for that one commit the panel
    // told the shell it was holding four unsaved fields before anything had been typed --
    // exactly the claim a section-switch confirm is built on (rule 146).
    //
    // Deterministic despite being one frame: the report goes through an effect, so the spy
    // records the call whether or not the frame was ever painted. Asserting on the RENDERED
    // bar could only ever catch it by luck.
    const { onDirtyChange } = renderReporting();
    await waitFor(() =>
      expect(screen.getByLabelText("Application name")).toHaveValue(STORED.application_name),
    );

    expect(onDirtyChange).not.toHaveBeenCalledWith(true);
  });

  it("never reports a draft when the stored row is already cached", async () => {
    // The warm twin of the test above (rule 72), and the reason it is worth writing even though
    // it passes on the first run: the cold test above passes for a reason that has nothing to do
    // with the guard. A fresh `QueryClient` leaves `general.data` undefined on the first render,
    // so nothing is compared yet. Returning to this section mounts the panel with the row already
    // there, and the boxes still on their initial values. `seeded` is `useState(false)`, which no
    // cached value can make true, so this holds -- and this test is what keeps it that way if the
    // flag is ever rewritten as one derived from the data (rule 145).
    const { onDirtyChange } = renderReporting(STORED);
    await waitFor(() =>
      expect(screen.getByLabelText("Application name")).toHaveValue(STORED.application_name),
    );

    expect(onDirtyChange).not.toHaveBeenCalledWith(true);
  });

  it("is absent until something is unsaved, and names what is", async () => {
    const person = renderPanel();
    const name = await screen.findByLabelText("Application name");
    // Rule 137, one turn earlier than usual: the box is not disabled here, it is UNSEEDED. The
    // form renders on the first data-bearing pass and the stored row is copied into local state
    // by an effect after it, so finding the box is not the same as the box holding what the
    // server sent. This asserted straight after the find and failed about one run in three
    // under load, reading as a bar that appears with nothing typed -- which is exactly what it
    // was, and #139 fixed the panel rather than the clock. The wait stays because it is the
    // right way to reach a seeded box, not because the bar needs time to go away.
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
    // #175. Success here was the savebar unmounting -- and it takes the button that had focus
    // with it, so the operator got no message, then no message, then a lost focus point. An
    // absence cannot be perceived by ear, which made a successful save and a dead button the
    // same event. The sentence is asserted through the live region, so this fails if the
    // `announce` call is dropped from the mutation OR if the region stops being reachable.
    const person = renderPanel();
    const name = await screen.findByLabelText("Application name");

    await fill(person, name, "Second install");
    await person.click(saveChanges());

    await waitFor(() => expect(announced()).toBe("Settings saved."));
    // On the server's answer, not on the press (rule 85): the bar is gone by the time it speaks.
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
    // be written -- and dropping just that field from a bar that names it would be a lie about
    // what the press did.
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
    // The preview overrode --accent and --accent-ink, so the button followed the typed color
    // and the LINK beside it kept the saved accent's ink -- a preview showing two accents at
    // once, on the control whose whole job is to show one.
    //
    // The link's color is --accent-text, and that is not derived from --accent where it is
    // used: the stylesheet computes it once on :root from what accent.ts writes there, so
    // overriding --accent on a child inherits an ink belonging to a different color. Asserted
    // as "all three move together" rather than against a transcribed hex, so the contrast
    // search stays free to return whatever it returns (rule 119).
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
    // #174. `aria-invalid` and `aria-describedby` appeared ZERO times in the whole frontend, so
    // a box that refuses to save had no way to reach the sentence explaining it: the message
    // was visible beside the field and the field never mentioned it. Asserted as the accessible
    // DESCRIPTION rather than as an id string, because that is what a reader actually computes
    // -- an id pointing at nothing would pass an attribute check and still say nothing.
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
    // Rule 39: the field it sent comes back from the response, trimmed the way the server
    // stored it -- so the row settles on what is really saved, not on what was typed.
    apiMock.saveGeneral.mockResolvedValue({ ...STORED, application_name: "Trimmed" });
    const person = renderPanel();
    const name = await screen.findByLabelText("Application name");

    await fill(person, name, "  Trimmed  ");
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
    // at the document foot -- off screen for anyone editing the top group, which is where five
    // of the six fields are. Rule 42: the reason renders where the failed press was.
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
    // STORED is Forever, but the length box seeds visible for the first paint and the effect
    // that reads default_spare_days:0 back into Forever removes it only after that commit.
    // Landing on Application name does not gate that effect, so assert the box's ABSENCE
    // through waitFor rather than synchronously, or the read can fall in the pre-seed window
    // and see the seed box carrying 30 (#477).
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

  it("stops taking presses while the save carrying it is in flight", async () => {
    // Issue #151, in the order it was driven. `Segmented` took no `disabled`, so this was the one
    // control in the row still pressable during a save -- every neighbor already carries
    // `disabled={save.isPending}`. The press flipped `aria-pressed`, so it read as taken; then
    // `onSuccess` re-seeded the mode from the response and the bar cleared in the same flush, so
    // nothing on screen said it had been dropped.
    //
    // What is pinned is the GAP, not the end state. After the response the mode reads Days either
    // way, because the re-seed is exactly what overwrote the press -- so the in-flight moment is
    // the only place a refused press and a silently dropped one look different (rule 118).
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

    // Both segments, not only the one that would have been pressed: the drop is symmetric, a
    // Days press during a save writing 0 was lost the same way.
    await waitFor(() => expect(forever()).toBeDisabled());
    expect(days()).toBeDisabled();

    // user-event dispatches nothing at a disabled target and reports success (rule 137), which
    // is the behavior under test: the pair does not move. It used to flip here.
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

// Generating REPLACES whatever key the server holds, immediately and with no undo, which is why
// Replace is a two-step confirm. The one-click Generate rendered on a CACHED `api_key_set: false`
// -- 30-second staleTime, no refetch on focus, nothing evicting it -- so a key made from another
// tab, a phone or by another admin left this panel offering a one-click revoke of a live key, with
// none of the confirmation its own design says the act needs (#203).
//
// The button proves the absence now instead of assuming it, so all three answers to "is there a
// key" are pinned here. Only the first one generates on one press: the other two are the states
// where the page cannot show that nothing is about to be destroyed.
describe("the display language picker", () => {
  it("names each shipped language in that language, and hands the pick to i18n", async () => {
    const person = renderPanel();
    const select = await screen.findByLabelText<HTMLSelectElement>("Language");

    // "Espanol", not "Spanish": an operator scanning for their language looks for the name they
    // call it (rule 21). The tag comes from the same glob the loader reads, so a translation
    // that ships is choosable with no edit here (rule 66) -- which is also why this asserts on
    // the two that ship rather than on the whole list.
    expect(select.textContent).toContain("Match my browser");
    expect(select.textContent).toContain("English");
    expect(select.textContent).toContain("Espa\u00f1ol");
    expect(select.textContent).not.toContain("Spanish");

    await person.selectOptions(select, "es");
    expect(setLanguageMock).toHaveBeenCalledWith("es");

    // Back to no choice at all, which is a different answer from picking English: it is what
    // lets the browser decide again.
    await person.selectOptions(select, "");
    expect(setLanguageMock).toHaveBeenCalledWith(undefined);
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

    // Another admin, another tab, a phone. This is the press that used to revoke it.
    apiMock.general.mockResolvedValue({ ...STORED, api_key_set: true });
    await user.click(button);

    await waitFor(() => expect(screen.getByText(/A key already exists/)).toBeInTheDocument());
    expect(apiMock.generateApiKey).not.toHaveBeenCalled();
    // And the row is now the key-present one, so the two-step Replace is what is on offer.
    expect(screen.getByRole("button", { name: "Replace…" })).toBeInTheDocument();
  });

  // One `confirmReplace` arms a danger button on BOTH rows, and `api_key_set` decides which row
  // renders, so the flag crossing between them arms a confirm nobody opened. Both directions are
  // driven here because both were reachable and they fail differently: one shows a danger button
  // with no notice, the other leaves a live key one press from revocation.
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

    // The no-key row rests on its plain one-click Generate. It used to open on a red "Confirm
    // generate", asking the operator to confirm destroying a key it had just proved is gone,
    // with no notice on the page saying why.
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

    // Now a key made in another tab lands: #203's own scenario, pointed the other way.
    apiMock.general.mockResolvedValue({ ...STORED, api_key_set: true });
    await act(async () => {
      await queryClient.invalidateQueries({ queryKey: ["general-settings"] });
    });

    // The key-present row rests on Replace…. It used to render with "Confirm replace" ALREADY
    // pressed, so one press revoked a live key through a confirm the operator never opened.
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

    // The sentence explained a confirm, so it goes when the confirm does. It used to stay,
    // telling the operator that confirming replaces a key with no confirm anywhere on the page.
    expect(screen.queryByText(/Couldn't check for an existing key/)).toBeNull();
  });

  it("says it is checking, and cannot be pressed again while it does", async () => {
    const user = renderPanel();
    const button = await screen.findByRole("button", { name: "Generate API key" });

    // Hold the re-read open. This is the window the button used to sit through under its idle
    // label, enabled: `generate` has not started yet, so the only pending flag it read was false.
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
    // B-18, in the shape it survives in: `onSuccess` re-seeded every field from the response, so
    // any save wrote the STORED url back over the one being typed. The per-row Save buttons that
    // first exposed it are gone, but the reverse-proxy switch still writes the moment it is
    // flipped, and it must not take the half-typed URL with it.
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

  /** Fold a tray/dock save back into the desktop object the way the server does, so the
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
