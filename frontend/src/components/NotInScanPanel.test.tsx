// SPDX-License-Identifier: AGPL-3.0-or-later
// The board's "not in the last scan" panel: it names every request the scan didn't include
// and groups them by why. It decides nothing; each row is static text.
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { UnmatchedRequest } from "../api";
import { expectNoA11yViolations } from "../test/a11y";
import { NotInScanPanel } from "./NotInScanPanel";
import { UnmatchedList } from "./UnmatchedList";

function u(over: Partial<UnmatchedRequest> = {}): UnmatchedRequest {
  return {
    title: "A Requested Film",
    year: 2021,
    media_type: "movie",
    is_4k: false,
    requested_at: "2026-03-02T00:00:00+00:00",
    available_at: null,
    reason: "set_aside",
    requested_by: ["Ada"],
    request_count: 1,
    ...over,
  };
}

describe("NotInScanPanel", () => {
  // This panel is the only place a request the scan skipped is named. Nothing here is clickable,
  // so if a reader loses the headings that group the reasons, the operator hears a flat list of
  // titles and no reason any of them were left out.
  it("has no accessibility violations", async () => {
    const { container } = render(
      <NotInScanPanel
        items={[
          u({ title: "New Arrival", reason: "after_scan" }),
          u({ title: "Kept Show", reason: "set_aside", media_type: "tv" }),
          u({ title: null, reason: "no_id" }),
        ]}
        onClose={vi.fn()}
      />,
    );
    await screen.findByText(/added since the last scan/i);
    await expectNoA11yViolations(container);
  });

  it("groups the requests under their reason, each with its own heading", () => {
    render(
      <NotInScanPanel
        items={[
          u({ title: "New Arrival", reason: "after_scan" }),
          u({ title: "Kept Show", reason: "set_aside", media_type: "tv" }),
          u({ title: null, reason: "no_id" }),
        ]}
        onClose={vi.fn()}
      />,
    );
    expect(screen.getByText(/added since the last scan/i)).toBeInTheDocument();
    expect(screen.getByText(/skipped by the last scan/i)).toBeInTheDocument();
    expect(screen.getByText(/couldn't be matched/i)).toBeInTheDocument();
    expect(screen.getByText("New Arrival")).toBeInTheDocument();
    expect(screen.getByText("Kept Show")).toBeInTheDocument();
  });

  it("falls back to a plain label when a title has no name, never an id", () => {
    render(<NotInScanPanel items={[u({ title: null, reason: "no_id" })]} onClose={vi.fn()} />);
    expect(screen.getByText(/name unavailable/i)).toBeInTheDocument();
  });

  it("reconciles the request count with the titles shown when co-requested", () => {
    render(
      <NotInScanPanel
        items={[u({ requested_by: ["Ada", "Bea"], request_count: 2 })]}
        onClose={vi.fn()}
      />,
    );
    // One title row, two requests behind it: the lead says both so nothing reads as missing.
    expect(screen.getByText(/2 requests across 1 title/i)).toBeInTheDocument();
  });

  it("closes on the close button and on Escape", async () => {
    const onClose = vi.fn();
    render(<NotInScanPanel items={[u()]} onClose={onClose} />);
    await userEvent.click(screen.getByRole("button", { name: /close/i }));
    await userEvent.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(2);
  });

  it("says it is checking, not all-clear, while the report is still loading", () => {
    render(<NotInScanPanel items={[]} isPending onClose={vi.fn()} />);
    expect(screen.getByText(/checking the last scan/i)).toBeInTheDocument();
    // The definite all-clear must not render when the data is merely missing (U-3, rule 17/36).
    expect(
      screen.queryByText(/every available request is in the last scan/i),
    ).not.toBeInTheDocument();
  });

  it("shows an error notice, not all-clear, when the report failed to load", () => {
    render(<NotInScanPanel items={[]} error onClose={vi.fn()} />);
    expect(screen.getByText(/couldn't read the last scan/i)).toBeInTheDocument();
    expect(
      screen.queryByText(/every available request is in the last scan/i),
    ).not.toBeInTheDocument();
  });

  it("shows the all-clear only when the report loaded and was genuinely empty", () => {
    render(<NotInScanPanel items={[]} onClose={vi.fn()} />);
    expect(screen.getByText(/every available request is in the last scan/i)).toBeInTheDocument();
  });
});

describe("UnmatchedList", () => {
  it("drops the current person from the asked-by line inside their own drawer", () => {
    render(<UnmatchedList items={[u({ requested_by: ["Ada", "Bea"] })]} excludeName="Ada" />);
    // Ada is the person whose drawer this is, so it reads "also asked by Bea", not "by Ada, Bea".
    expect(screen.getByText(/also asked by Bea/i)).toBeInTheDocument();
    expect(screen.queryByText(/by Ada/i)).not.toBeInTheDocument();
  });

  it("lists an unrecognized reason under a catch-all so nothing is dropped", () => {
    render(<UnmatchedList items={[u({ title: "Odd One", reason: "brand_new_code" })]} />);
    expect(screen.getByText("Odd One")).toBeInTheDocument();
  });
});

// The panel is a full-screen dialog under 900px, so it has to say what it is. Its name comes from
// its own <h2> rather than a second copy of the title in an aria-label (rule 144). One of these
// per panel: six surfaces render WhyShell, and the name is the one part of the contract the shell
// cannot supply for them.
describe("the not-in-scan panel's accessible name", () => {
  it("names itself from its own heading", () => {
    render(<NotInScanPanel items={[u()]} onClose={vi.fn()} />);
    expect(screen.getByRole("complementary", { name: "Not in the last scan" })).toBeInTheDocument();
  });
});
