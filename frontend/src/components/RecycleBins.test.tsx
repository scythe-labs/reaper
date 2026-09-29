// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { ApiError, type RunBin } from "../api";
import { expectNoA11yViolations } from "../test/a11y";
import { renderWithProviders } from "../test/renderWithProviders";
import {
  ConfirmBins,
  RecycleBinBanner,
  RunBinsList,
  binDays,
  freedNow,
  skipPhrase,
} from "./RecycleBins";

const { apiMock } = await vi.hoisted(async () => ({
  apiMock: (await import("../test/apiMock")).makeApiMock(),
}));

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  api: apiMock,
}));

const TIB = 1024 ** 4;

/** The confirm sheet holds the ticked set. This stands in for it. */
function Harness({ runId }: { runId: number }) {
  const [skip, setSkip] = useState<ReadonlySet<string>>(new Set());
  return (
    <ConfirmBins
      runId={runId}
      skip={skip}
      onSkip={(key, on) =>
        setSkip((prev) => {
          const next = new Set(prev);
          if (on) next.add(key);
          else next.delete(key);
          return next;
        })
      }
    />
  );
}

function bin(overrides: Partial<RunBin>): RunBin {
  return {
    kind: "radarr",
    instance_id: 1,
    name: "Radarr",
    bin: "on",
    cleanup_days: 3,
    items: 180,
    bytes: 2 * TIB,
    skipped: false,
    state: null,
    ...overrides,
  };
}

describe("freedNow and binDays", () => {
  it("subtracts only what a bin holds", () => {
    expect(freedNow(10, 4)).toBe(6);
    expect(freedNow(10, null)).toBe(10);
  });

  it("promises the slowest bin's days, and no date when a bin has none", () => {
    expect(binDays([bin({ cleanup_days: 3 }), bin({ cleanup_days: 7 })])).toBe(7);
    expect(binDays([bin({ cleanup_days: 3 }), bin({ bin: "none", cleanup_days: null })])).toBe(3);
    expect(binDays([bin({ cleanup_days: 3 }), bin({ cleanup_days: 0 })])).toBeNull();
    expect(binDays([bin({ cleanup_days: null })])).toBeNull();
  });

  it("knows no date when no bin is on or one could not be read", () => {
    expect(binDays([bin({ bin: "none", cleanup_days: null })])).toBeUndefined();
    expect(
      binDays([bin({ cleanup_days: 3 }), bin({ bin: "unknown", cleanup_days: null })]),
    ).toBeUndefined();
  });
});

describe("the reap confirm's recycle bin list", () => {
  it("names each instance, when its space frees, and the total", async () => {
    apiMock.runBins.mockResolvedValue({
      bins: [
        bin({}),
        bin({
          kind: "sonarr",
          instance_id: 2,
          name: "Sonarr",
          cleanup_days: 7,
          items: 1,
          bytes: TIB,
        }),
        bin({ instance_id: 3, name: "Radarr 4K", bin: "none", items: 2, bytes: TIB }),
      ],
    });
    const { container } = renderWithProviders(<Harness runId={7} />);

    expect(await screen.findByText("Recycle bins")).toBeInTheDocument();
    expect(screen.getByText("180 titles, 2.0 TiB")).toBeInTheDocument();
    expect(screen.getByText("1 season, 1.0 TiB")).toBeInTheDocument();
    expect(screen.getByText("Frees in 3 days")).toBeInTheDocument();
    expect(screen.getByText("No bin, frees now")).toBeInTheDocument();
    expect(
      screen.getByText("1.0 TiB frees now. 3.0 TiB frees when the bins empty, within 7 days."),
    ).toBeInTheDocument();

    await expectNoA11yViolations(container);
  });

  it("says nothing when no instance has a bin", async () => {
    apiMock.runBins.mockResolvedValue({ bins: [bin({ bin: "none" })] });
    const { container } = renderWithProviders(<Harness runId={7} />);

    await vi.waitFor(() => expect(apiMock.runBins).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it("drops the total when a bin could not be read", async () => {
    apiMock.runBins.mockResolvedValue({
      bins: [bin({}), bin({ instance_id: 2, name: "Radarr 4K", bin: "unknown" })],
    });
    renderWithProviders(<Harness runId={7} />);

    expect(await screen.findByText("Couldn't read its bin")).toBeInTheDocument();
    expect(screen.queryByText(/frees when the bins empty/)).toBeNull();
  });

  it("says it could not read the bins when the request fails", async () => {
    apiMock.runBins.mockRejectedValue(new Error("down"));
    renderWithProviders(<Harness runId={7} />);

    expect(await screen.findByText("Reaper couldn't read the recycle bins.")).toBeInTheDocument();
  });
});

describe("the confirm reads the bins again", () => {
  it("asks the servers again when the confirm opens a second time", async () => {
    apiMock.runBins.mockReset();
    apiMock.runBins.mockResolvedValueOnce({ bins: [bin({ bin: "unknown" })] });
    apiMock.runBins.mockResolvedValueOnce({ bins: [bin({})] });
    const first = renderWithProviders(<Harness runId={7} />);
    await screen.findByText("Couldn't read its bin");
    first.unmount();

    renderWithProviders(<Harness runId={7} />, { client: first.client });

    expect(await screen.findByText("Frees in 3 days")).toBeInTheDocument();
    expect(apiMock.runBins).toHaveBeenCalledTimes(2);
  });
});

describe("a past run's recycle bin list", () => {
  it("says what each instance did with the files", async () => {
    apiMock.runBins.mockResolvedValue({
      bins: [
        bin({ cleanup_days: 0 }),
        bin({ kind: "sonarr", instance_id: 2, name: "Sonarr", bin: "none" }),
      ],
    });
    renderWithProviders(<RunBinsList runId={16} />);

    expect(await screen.findByText("bin on, 2.0 TiB frees when the bin is emptied")).toBeVisible();
    expect(screen.getByText("no recycle bin")).toBeVisible();
  });

  it("says it could not read the bins when the request fails", async () => {
    apiMock.runBins.mockRejectedValue(new Error("down"));
    renderWithProviders(<RunBinsList runId={16} />);

    expect(await screen.findByText("Reaper couldn't read the recycle bins.")).toBeVisible();
  });
});

describe("skipping a bin", () => {
  it("adds the bins to the phrase the way the server does", () => {
    // test_the_phrase_names_the_bins_it_turns_off pins the server's copy of this suffix.
    expect(skipPhrase("REAP 1 SOUL 100 GB", 0)).toBe("REAP 1 SOUL 100 GB");
    expect(skipPhrase("REAP 1 SOUL 100 GB", 1)).toBe("REAP 1 SOUL 100 GB SKIP 1 BIN");
    expect(skipPhrase("REAP 1 SOUL 100 GB", 2)).toBe("REAP 1 SOUL 100 GB SKIP 2 BINS");
  });

  it("frees a ticked instance's space now and says there is no undo", async () => {
    apiMock.runBins.mockResolvedValue({
      bins: [bin({}), bin({ kind: "sonarr", instance_id: 2, name: "HD", cleanup_days: 7 })],
    });
    const user = userEvent.setup();
    renderWithProviders(<Harness runId={7} />);

    await user.click(
      await screen.findByRole("checkbox", {
        name: "Skip Radarr Radarr's recycle bin for this reap",
      }),
    );

    expect(screen.getByText("Frees now, no undo")).toBeInTheDocument();
    expect(
      screen.getByText("2.0 TiB frees now. 2.0 TiB frees when the bins empty, within 7 days."),
    ).toBeInTheDocument();
    expect(screen.getByText(/deleted for good/)).toBeInTheDocument();
  });

  it("offers no box for an instance with no bin", async () => {
    apiMock.runBins.mockResolvedValue({
      bins: [bin({}), bin({ instance_id: 3, name: "4K", bin: "none" })],
    });
    renderWithProviders(<Harness runId={7} />);

    expect(await screen.findAllByRole("checkbox")).toHaveLength(1);
  });

  it("reports whether each skipped bin came back", async () => {
    apiMock.runBins.mockResolvedValue({
      bins: [
        bin({ skipped: true, state: "restored" }),
        bin({ instance_id: 2, name: "4K", skipped: true, state: "off" }),
        bin({ instance_id: 3, name: "Other", skipped: true, state: "left" }),
      ],
    });
    renderWithProviders(<RunBinsList runId={16} />);

    expect(await screen.findByText("skipped for this reap, back on")).toBeVisible();
    expect(screen.getByText("skipped for this reap, still off")).toBeVisible();
    expect(screen.getByText("changed during the reap, left as you set it")).toBeVisible();
  });
});

describe("the banner for a bin still off", () => {
  const off = { kind: "sonarr", instance_id: 2, name: "HD", run_id: 16 };

  it("names the bin and says it is still off after a failed retry", async () => {
    apiMock.recycleBinsOff.mockResolvedValue({ bins: [off] });
    apiMock.restoreRecycleBins.mockResolvedValue({ bins: [off] });
    const user = userEvent.setup();
    renderWithProviders(<RecycleBinBanner />);

    expect(
      await screen.findByText(
        /Sonarr HD's recycle bin is still off. Reaper couldn't turn it back on after reap 16./,
      ),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Turn it back on" }));

    expect(await screen.findByText(/Still couldn't reach Sonarr HD/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeEnabled();
  });

  it("shows the server's refusal when the retry itself fails", async () => {
    apiMock.recycleBinsOff.mockResolvedValue({ bins: [off] });
    apiMock.restoreRecycleBins.mockRejectedValue(
      new ApiError(409, "busy", "error.runs.already_running"),
    );
    const user = userEvent.setup();
    renderWithProviders(<RecycleBinBanner />);

    await user.click(await screen.findByRole("button", { name: "Turn it back on" }));

    expect(await screen.findByText(/A reap is already running/)).toBeInTheDocument();
    expect(screen.queryByText(/Still couldn't reach/)).not.toBeInTheDocument();
  });

  it("goes away once the bin is back", async () => {
    apiMock.recycleBinsOff.mockResolvedValue({ bins: [off] });
    apiMock.restoreRecycleBins.mockResolvedValue({ bins: [] });
    const user = userEvent.setup();
    const { container } = renderWithProviders(<RecycleBinBanner />);

    await user.click(await screen.findByRole("button", { name: "Turn it back on" }));

    await vi.waitFor(() => expect(container).toBeEmptyDOMElement());
  });
});
