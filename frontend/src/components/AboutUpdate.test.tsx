// SPDX-License-Identifier: AGPL-3.0-or-later
// The update surfaces on About: the version pill, the Update row's one sentence per
// state, the dev-build banner, and the what-changed modal. The property pinned across
// every case: a check that could not answer renders quiet muted text, never an error
// and never a nag, because nothing gates on this surface.
import { QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { About, Update } from "../api";
import { expectNoA11yViolations } from "../test/a11y";
import { DEFAULT_UPDATE } from "../test/apiFixtures";
import { testQueryClient } from "../test/queryClient";
import { AboutPanel } from "./Settings";

const { apiMock } = vi.hoisted(() => ({
  apiMock: {
    about: vi.fn(),
    update: vi.fn(),
  },
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
  return render(
    <QueryClientProvider client={testQueryClient()}>
      <AboutPanel />
    </QueryClientProvider>,
  );
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

  it("shows the dev banner and links the branch's run of changes", async () => {
    apiMock.update.mockResolvedValue({
      ...NEWER,
      channel: "dev",
      current: "dev (abc1234)",
      latest: "dev (def5678)",
      url: "https://github.com/scythe-labs/reaper/commits/dev",
      changes: [],
    });
    renderAbout();

    // The banner is page furniture (standing), so it is read in document order
    // rather than announced; asserting the text is the whole contract.
    expect(await screen.findByText(/It changes daily and can break/)).toBeInTheDocument();
    expect(await screen.findByText("Newer dev build")).toBeInTheDocument(); // the pill
    expect(await screen.findByRole("link", { name: "See what changed" })).toHaveAttribute(
      "href",
      "https://github.com/scythe-labs/reaper/commits/dev",
    );
  });

  it("keeps the last answer through a failed refetch, on every surface", async () => {
    // React Query retains the last good data and raises isError beside it. The row
    // renders the retained answer exactly like the pill and the chip light do, or
    // the pill would say "Update available" directly above a row claiming the
    // check failed.
    apiMock.update.mockResolvedValue(NEWER);
    const client = testQueryClient();
    render(
      <QueryClientProvider client={client}>
        <AboutPanel />
      </QueryClientProvider>,
    );
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
    expect(screen.queryByText(/It changes daily/)).not.toBeInTheDocument();
  });

  it("says checks are off without asking anything", async () => {
    apiMock.update.mockResolvedValue({ ...DEFAULT_UPDATE, enabled: false });
    renderAbout();
    expect(await screen.findByText(/Update checks are off/)).toBeInTheDocument();
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
