// SPDX-License-Identifier: AGPL-3.0-or-later
// The update surfaces on About: the version pill, the Update row's one sentence per
// state, the dev-build banner, and the what-changed modal. The property pinned across
// every case: a check that could not answer renders quiet muted text, never an error
// and never a nag, because nothing gates on this surface.
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { About, Update } from "../api";
import en from "../locales/en/ui.json";
import { expectNoA11yViolations } from "../test/a11y";
import { DEFAULT_UPDATE } from "../test/apiFixtures";
import { testQueryClient } from "../test/queryClient";
import { renderWithProviders } from "../test/renderWithProviders";
import { AboutPanel } from "./AboutPanel";

const { apiMock } = await vi.hoisted(async () => ({
  apiMock: (await import("../test/apiMock")).makeApiMock(),
}));

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  api: apiMock,
}));

const ABOUT: About = {
  version: "2026.8.1",
  license: "AGPL-3.0-or-later",
  data_dir: "/data",
  reaper_db_bytes: 1024,
  cache_db_bytes: 2048,
};

const RELEASE_URL = "https://github.com/scythe-labs/reaper/releases/tag/v2026.9.1";

const NEWER: Update = {
  channel: "release",
  enabled: true,
  current: "2026.8.1",
  latest: "2026.9.1",
  update_available: true,
  url: RELEASE_URL,
  checked_at: "2026-08-02T12:00:00+00:00",
  changes: [
    {
      version: "2026.9.1",
      url: RELEASE_URL,
      notes: "## Fixed\n* the reap ledger counted spares",
    },
  ],
};

function renderAbout() {
  return renderWithProviders(<AboutPanel />);
}

beforeEach(() => {
  vi.clearAllMocks();
  apiMock.about.mockResolvedValue(ABOUT);
  apiMock.update.mockResolvedValue(DEFAULT_UPDATE);
});

describe("the About update row", () => {
  it("offers the new release and opens its changelog in the modal", async () => {
    apiMock.update.mockResolvedValue(NEWER);
    const person = userEvent.setup();
    renderAbout();

    expect(await screen.findByText("Update available")).toBeInTheDocument(); // the pill
    expect(await screen.findByText(/Reaper 2026\.9\.1 is out/)).toBeInTheDocument();
    // The sentence beneath points at the schedule that now drives the check (the
    // `check_for_updates` job), and names no cadence of its own, because the operator can
    // change the cron or turn it off (rule 86). It read "Reaper checks a few times a day"
    // while nothing checked on its own at all (#464, rule 25).
    expect(screen.getByText(/checks on a schedule you can change in Jobs/)).toBeInTheDocument();

    await person.click(screen.getByRole("button", { name: "See what changed" }));
    const dialog = await screen.findByRole("dialog", { name: "What changed" });
    expect(within(dialog).getByRole("heading", { name: "Reaper 2026.9.1" })).toBeInTheDocument();
    // A markdown list item as a rendered list item, not as literal asterisk text.
    expect(within(dialog).getByRole("listitem")).toHaveTextContent(
      "the reap ledger counted spares",
    );
    expect(within(dialog).getByRole("link", { name: "View on GitHub" })).toHaveAttribute(
      "href",
      RELEASE_URL,
    );
    // The richest tree this file mounts: pill, row, and the modal with rendered markdown.
    await expectNoA11yViolations();
  });

  it("carries every release behind, so the middle one is not lost", async () => {
    apiMock.update.mockResolvedValue({
      ...NEWER,
      latest: "2026.9.2",
      changes: [
        { version: "2026.9.2", url: null, notes: "second" },
        { version: "2026.9.1", url: null, notes: "first" },
      ],
    });
    const person = userEvent.setup();
    renderAbout();

    await person.click(await screen.findByRole("button", { name: "See what changed" }));
    const dialog = await screen.findByRole("dialog", { name: "What changed" });
    const headings = within(dialog).getAllByRole("heading", { level: 3 });
    expect(headings.map((h) => h.textContent)).toEqual(["Reaper 2026.9.2", "Reaper 2026.9.1"]);
  });

  it("shows the dev banner and links the commits an update would bring", async () => {
    apiMock.update.mockResolvedValue({
      ...NEWER,
      channel: "dev",
      current: "dev (abc1234)",
      latest: "dev (def5678)",
      // The server sends the range between this build and the published :dev image, so
      // the page the operator lands on holds only what they do not have yet. Following
      // the branch instead put every docs commit on that page and announced an update
      // no image existed for.
      url: "https://github.com/scythe-labs/reaper/compare/abc1234...def5678",
      changes: [],
    });
    renderAbout();

    // The banner is page furniture (standing), so it is read in document order
    // rather than announced; asserting the text is the whole contract.
    expect(await screen.findByText(/It changes often and can break/)).toBeInTheDocument();
    // One pill on both channels: the operator's question is the same either way.
    expect(await screen.findByText("Update available")).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: "See what changed" })).toHaveAttribute(
      "href",
      "https://github.com/scythe-labs/reaper/compare/abc1234...def5678",
    );
  });

  it("reports the published dev image as already taken", async () => {
    apiMock.update.mockResolvedValue({
      ...NEWER,
      channel: "dev",
      current: "dev (abc1234)",
      latest: "dev (abc1234)",
      update_available: false,
      url: null,
      changes: [],
    });
    renderAbout();
    expect(await screen.findByText("You are on the latest dev build.")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText("Update available")).not.toBeInTheDocument());
  });

  it("keeps the last answer through a failed refetch, on every surface", async () => {
    // React Query retains the last good data and raises isError beside it. The row
    // renders the retained answer exactly like the pill and the chip light do, or
    // the pill would say "Update available" directly above a row claiming the
    // check failed.
    apiMock.update.mockResolvedValue(NEWER);
    const client = testQueryClient();
    renderWithProviders(<AboutPanel />, { client });
    expect(await screen.findByText(/Reaper 2026\.9\.1 is out/)).toBeInTheDocument();

    apiMock.update.mockRejectedValue(new Error("api restarting"));
    await act(async () => {
      await client.refetchQueries({ queryKey: ["update"] });
    });
    expect(screen.getByText(/Reaper 2026\.9\.1 is out/)).toBeInTheDocument();
    expect(screen.getByText("Update available")).toBeInTheDocument();
    expect(screen.queryByText(/Couldn't check for updates/)).not.toBeInTheDocument();
  });

  it("reads as quiet unknown when the check could not answer", async () => {
    renderAbout(); // DEFAULT_UPDATE: enabled, unanswered
    expect(await screen.findByText(/Couldn't check for updates/)).toBeInTheDocument();
    expect(screen.queryByText("Update available")).not.toBeInTheDocument();
    expect(screen.queryByText(/It changes often/)).not.toBeInTheDocument();
  });

  it("says checks are off without asking anything", async () => {
    apiMock.update.mockResolvedValue({ ...DEFAULT_UPDATE, enabled: false });
    renderAbout();
    expect(await screen.findByText(/Update checks are off/)).toBeInTheDocument();
    // The way back on names launcher.conf first: a double-clicked app exposes no
    // environment the operator can edit, and the conf template is where the manual
    // and the first-run file both put this line.
    expect(screen.getByText(/launcher\.conf in Reaper's data folder/)).toBeInTheDocument();
  });

  it("reports the newest release as already taken", async () => {
    apiMock.update.mockResolvedValue({
      ...NEWER,
      latest: "2026.8.1",
      update_available: false,
      changes: [],
    });
    renderAbout();
    expect(await screen.findByText("You are on the newest release.")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText("Update available")).not.toBeInTheDocument());
  });
});

describe("jobs.result.update_available says the same thing as this row's about.update.newRelease (rule 144)", () => {
  // Three of the four states this row used to word on its own (devMoved, devCurrent,
  // releaseCurrent) now read `jobs.result.update_dev_behind` / `update_dev_current` /
  // `update_up_to_date` directly, so those three cannot drift: there is only one catalog
  // entry left to read. `update_available` stays a separate sibling on purpose: the
  // backend fixes its param name to `{latest}` (`Reason("update_available", {"latest": ...})`
  // in `services/scheduler.py`), while this row's own update query supplies `{version}` from
  // its own endpoint, so this pins the two by name rather than trusting the wording agrees.
  // Compared with the trailing period and the param name stripped, since `about.update.*` is
  // a standalone sentence and `jobs.result.update_*` is a resting-line fragment
  // (`jobs.status.lastRunOk`/`lastRunFailedReason` supply their own punctuation).
  const strip = (s: string) => s.replace(/\.$/, "").replace(/\{[^}]+\}/g, "{}");

  it("matches the one state that's still a separate sibling", () => {
    expect(strip(en.jobs.result.update_available)).toBe(strip(en.about.update.newRelease));
  });
});
