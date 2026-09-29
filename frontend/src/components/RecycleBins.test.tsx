// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { RunBin } from "../api";
import { expectNoA11yViolations } from "../test/a11y";
import { renderWithProviders } from "../test/renderWithProviders";
import { ConfirmBins, RunBinsList, binDays, freedNow } from "./RecycleBins";

const { apiMock } = await vi.hoisted(async () => ({
  apiMock: (await import("../test/apiMock")).makeApiMock(),
}));

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  api: apiMock,
}));

const TIB = 1024 ** 4;

function bin(overrides: Partial<RunBin>): RunBin {
  return {
    kind: "radarr",
    instance_id: 1,
    name: "Radarr",
    bin: "on",
    cleanup_days: 3,
    items: 180,
    bytes: 2 * TIB,
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
    const { container } = renderWithProviders(<ConfirmBins runId={7} />);

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
    const { container } = renderWithProviders(<ConfirmBins runId={7} />);

    await vi.waitFor(() => expect(apiMock.runBins).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it("drops the total when a bin could not be read", async () => {
    apiMock.runBins.mockResolvedValue({
      bins: [bin({}), bin({ instance_id: 2, name: "Radarr 4K", bin: "unknown" })],
    });
    renderWithProviders(<ConfirmBins runId={7} />);

    expect(await screen.findByText("Couldn't read its bin")).toBeInTheDocument();
    expect(screen.queryByText(/frees when the bins empty/)).toBeNull();
  });

  it("says it could not read the bins when the request fails", async () => {
    apiMock.runBins.mockRejectedValue(new Error("down"));
    renderWithProviders(<ConfirmBins runId={7} />);

    expect(await screen.findByText("Reaper couldn't read the recycle bins.")).toBeInTheDocument();
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
});
