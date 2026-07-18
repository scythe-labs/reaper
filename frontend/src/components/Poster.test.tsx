// SPDX-License-Identifier: AGPL-3.0-or-later
// The queue's poster tile. One behavior is load-bearing: a failed image must not latch.
// Art is fetched per item from a same-origin route that rides the session cookie, so a
// single hiccup (an expired session, a slow Plex) fails one <img>. Because the queue
// re-uses these rows as the list re-sorts and pages, a "broken" flag that never resets
// would spread that one failure across every item the row is later used for, and the art
// would stay gone until a full remount. The reset below is what stops that.
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Poster } from "./ReviewQueue";

describe("the poster tile", () => {
  it("shows the art when it loads", () => {
    render(<Poster url="/api/poster/1" alt="Example Show" />);
    expect(screen.getByRole("img", { name: "Example Show" })).toBeInTheDocument();
  });

  it("falls back to the placeholder when the image fails", () => {
    render(<Poster url="/api/poster/1" alt="Example Show" />);
    fireEvent.error(screen.getByRole("img", { name: "Example Show" }));
    expect(screen.queryByRole("img", { name: "Example Show" })).not.toBeInTheDocument();
  });

  it("tries again for the next item rather than staying broken", () => {
    const { rerender } = render(<Poster url="/api/poster/1" alt="Example Show" />);
    fireEvent.error(screen.getByRole("img", { name: "Example Show" }));
    expect(screen.queryByRole("img", { name: "Example Show" })).not.toBeInTheDocument();

    // The same row, now showing a different item: the art must be attempted again.
    rerender(<Poster url="/api/poster/2" alt="Another Show" />);
    expect(screen.getByRole("img", { name: "Another Show" })).toBeInTheDocument();
  });

  it("shows the placeholder when there is no art to fetch", () => {
    render(<Poster url={null} alt="Example Show" />);
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });
});
