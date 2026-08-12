// SPDX-License-Identifier: AGPL-3.0-or-later
// Two things the log viewer must not get wrong: it may only claim to be retrying when a
// retry is actually scheduled ("Follow new lines" is the only thing that schedules one), and
// every option in the level filter has to filter something different from its neighbors.
import { act, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { Announcer } from "../announce";
import { expectNoA11yViolations } from "../test/a11y";
import { testQueryClient } from "../test/queryClient";
import { renderWithProviders } from "../test/renderWithProviders";
import { LogsPanel } from "./LogsPanel";

const { apiMock } = await vi.hoisted(async () => ({
  apiMock: (await import("../test/apiMock")).makeApiMock(),
}));

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  api: apiMock,
}));

function page(seq: number) {
  return {
    level: "INFO",
    last_seq: seq,
    files_kept: 3,
    lines: [{ seq, ts: "2026-01-01T00:00:00+00:00", level: "INFO", text: "a line" }],
  };
}

function renderPanel() {
  const queryClient = testQueryClient();
  renderWithProviders(
    <>
      {/* The app mounts this above every route (`App.tsx`), and `announce()` returns early when
          no region is listening -- so without it here Try again's sentence is dropped and a test
          about it passes against silence. */}
      <Announcer />
      <LogsPanel />
    </>,
    { client: queryClient },
  );
  return { queryClient, person: userEvent.setup() };
}

describe("LogsPanel", () => {
  beforeEach(() => {
    apiMock.logs.mockReset();
    apiMock.logs.mockResolvedValue(page(1));
    apiMock.downloadLogs.mockReset();
    apiMock.downloadLogs.mockResolvedValue(undefined);
  });

  // This is where an operator goes when a scan or a reap went wrong, and the level filter and
  // the follow switch are the only controls on it. A filter a screen reader cannot name leaves
  // them scrolling raw lines for the one that explains what happened.
  it("has no accessibility violations", async () => {
    renderPanel();
    await screen.findByText("a line");
    await expectNoA11yViolations();
  });

  it("offers no level that filters exactly what the one above it does", () => {
    renderPanel();
    const filter = screen.getByLabelText(/only show this level and up/i);
    const options = Array.from(filter.querySelectorAll("option")).map((o) => o.textContent);
    expect(options).toEqual(["All levels", "Info and up", "Warnings and up", "Errors only"]);
  });

  it("offers Try again, and does not claim to be retrying, once following is off", async () => {
    const { queryClient, person } = renderPanel();
    expect(await screen.findByText("a line")).toBeInTheDocument();

    apiMock.logs.mockRejectedValue(new Error("boom"));
    await person.click(screen.getByRole("switch", { name: /follow new lines/i }));
    await act(() => queryClient.refetchQueries({ queryKey: ["logs"] }));

    expect(await screen.findByText(/updates are paused/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /try again/i })).toBeInTheDocument();
    expect(screen.queryByText(/trying again/i)).not.toBeInTheDocument();
  });

  it("says it is trying again while following is on", async () => {
    const { queryClient } = renderPanel();
    expect(await screen.findByText("a line")).toBeInTheDocument();

    apiMock.logs.mockRejectedValue(new Error("boom"));
    await act(() => queryClient.refetchQueries({ queryKey: ["logs"] }));

    expect(await screen.findByText(/reaper is trying again/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /try again/i })).not.toBeInTheDocument();
  });

  // #376: the pair was wrong in both directions at once -- it spoke when nobody did anything,
  // and stayed silent when someone did. Both halves are pinned, because fixing either alone
  // leaves the other exactly as broken and neither is visible from the diff.
  it("does not announce a failure the poll found, in either notice", async () => {
    const { queryClient } = renderPanel();
    expect(await screen.findByText("a line")).toBeInTheDocument();

    apiMock.logs.mockRejectedValue(new Error("boom"));
    await act(() => queryClient.refetchQueries({ queryKey: ["logs"] }));

    // `["logs"]` refetches every 2s, so a flapping connection remounts this notice on a cycle
    // no operator drove. As an alert it re-read byte-identical text over whoever was reading
    // the pane or typing in the search box.
    const notice = await screen.findByText(/reaper is trying again/i);
    expect(notice.closest(".notice")).not.toHaveAttribute("role", "alert");
  });

  it("announces the outcome of Try again, whichever way it goes", async () => {
    const { queryClient, person } = renderPanel();
    expect(await screen.findByText("a line")).toBeInTheDocument();

    apiMock.logs.mockRejectedValue(new Error("boom"));
    await person.click(screen.getByRole("switch", { name: /follow new lines/i }));
    await act(() => queryClient.refetchQueries({ queryKey: ["logs"] }));
    expect(await screen.findByText(/updates are paused/i)).toBeInTheDocument();

    // Failing again is the case that used to say nothing at all: `isError` stays true across
    // the retry, so the notice never unmounts and its text never changes. Nothing about the
    // rendered page distinguishes a retry that failed from a button that does nothing.
    await person.click(screen.getByRole("button", { name: /try again/i }));
    await screen.findByText(/the log still didn't load\./i);

    apiMock.logs.mockResolvedValue(page(2));
    await person.click(screen.getByRole("button", { name: /try again/i }));
    expect(await screen.findByText(/the log is up to date\./i)).toBeInTheDocument();
  });

  it("downloads the full log on demand", async () => {
    const { person } = renderPanel();
    await person.click(screen.getByRole("button", { name: /download logs/i }));
    expect(apiMock.downloadLogs).toHaveBeenCalledOnce();
  });

  it("shows a plain error when the download won't start", async () => {
    apiMock.downloadLogs.mockRejectedValue(new Error("Couldn't reach the server."));
    const { person } = renderPanel();
    await person.click(screen.getByRole("button", { name: /download logs/i }));
    expect(await screen.findByText(/the download didn't start/i)).toBeInTheDocument();
  });

  it("renders the retention count from the server, not a hardcoded number (I-6)", async () => {
    apiMock.logs.mockResolvedValue(page(1)); // files_kept: 3
    renderPanel();
    expect(await screen.findByText(/keeps the newest 3 files/i)).toBeInTheDocument();
  });

  async function levelPicker() {
    const picker = await screen.findByLabelText<HTMLSelectElement>("Logging level");
    // It is disabled until the first read lands, so this is also the wait for the level
    // the server reported (rule 137).
    await waitFor(() => expect(picker).toBeEnabled());
    return picker;
  }

  function optionsOf(picker: HTMLSelectElement) {
    return Array.from(picker.querySelectorAll("option")).map((o) => o.textContent);
  }

  // #700: REAPER_LOG_LEVEL may carry ERROR, which this picker does not offer on purpose --
  // hiding warnings from a tool that deletes files serves nobody. A <select> whose value
  // matches no option renders blank, so without the extra option the one surface that says
  // what Reaper is recording goes silent for exactly the operator who chose it. Both
  // directions, since an option rendered unconditionally would put the choice on sale.
  it("offers three levels while Reaper is recording at one of them", async () => {
    renderPanel();

    expect(optionsOf(await levelPicker())).toEqual(["Debug", "Info", "Warning"]);
  });

  it("names the recording level even when it is one the picker does not offer", async () => {
    apiMock.logs.mockResolvedValue({ ...page(1), level: "ERROR" });
    renderPanel();

    const picker = await levelPicker();

    expect(optionsOf(picker)).toEqual(["Debug", "Info", "Warning", "Error"]);
    expect(picker.value).toBe("ERROR");
  });
});
