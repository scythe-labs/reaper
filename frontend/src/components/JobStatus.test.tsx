// SPDX-License-Identifier: AGPL-3.0-or-later
// The one status line every job wears. These pin the resting states (a success, a failure,
// a job that has never run), the running spinner, and -- the point of the row -- that a
// manual run flashes a short confirmation on the running->done transition and then settles
// back to the resting line, all inside one fixed slot so nothing below it moves.
import { act, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { JobStatus, type JobFlash, useJobFlash } from "./JobStatus";

const AT = "2026-07-24T03:30:00Z";

describe("JobStatus resting states", () => {
  it("shows a green dot and a last-run line for a job that succeeded", () => {
    const { container } = render(
      <JobStatus running={false} runningLabel="" lastRunAt={AT} lastOk={true} flash={null} />,
    );
    expect(container.querySelector(".last-dot.ok")).not.toBeNull();
    const line = container.querySelector(".jobrow-last");
    expect(line?.className).not.toContain("is-fail");
    expect(line?.textContent).toContain("Last run");
  });

  it("marks a failed run in red, without the word 'failed' being lost", () => {
    const { container } = render(
      <JobStatus running={false} runningLabel="" lastRunAt={AT} lastOk={false} flash={null} />,
    );
    expect(container.querySelector(".last-dot.fail")).not.toBeNull();
    const line = container.querySelector(".jobrow-last");
    expect(line?.className).toContain("is-fail");
    expect(line?.textContent).toContain("Last run failed");
  });

  it("appends the plain-language reason beside the exact time for a failed run", () => {
    const { container } = render(
      <JobStatus
        running={false}
        runningLabel=""
        lastRunAt={AT}
        lastOk={false}
        lastResult="Couldn't refresh ratings"
        flash={null}
      />,
    );
    expect(container.querySelector(".last-exact")?.textContent).toContain(
      "Couldn't refresh ratings",
    );
  });

  it("never shows the reason beside a run that did not fail", () => {
    const { container } = render(
      <JobStatus
        running={false}
        runningLabel=""
        lastRunAt={AT}
        lastOk={true}
        lastResult="Ratings refreshed"
        flash={null}
      />,
    );
    expect(container.querySelector(".last-exact")?.textContent).not.toContain("Ratings refreshed");
  });

  it("reads 'hasn't run yet' with a hollow dot when there is no last run", () => {
    const { container } = render(
      <JobStatus running={false} runningLabel="" lastRunAt={null} lastOk={null} flash={null} />,
    );
    expect(container.querySelector(".last-dot.never")).not.toBeNull();
    expect(container.querySelector(".jobrow-last")?.textContent).toContain("Hasn't run yet");
  });

  it("shows the spinner and the running label while the job runs", () => {
    const { container } = render(
      <JobStatus
        running={true}
        runningLabel="Reading watch history · 40%"
        lastRunAt={AT}
        lastOk={true}
        flash={null}
      />,
    );
    expect(container.querySelector(".spin")).not.toBeNull();
    expect(container.querySelector(".jobrow-run")?.textContent).toContain(
      "Reading watch history · 40%",
    );
  });
});

function Harness({ running, result }: { running: boolean; result: JobFlash | null }) {
  const flash = useJobFlash(running, result);
  return (
    <JobStatus running={running} runningLabel="Running now…" lastRunAt={AT} lastOk flash={flash} />
  );
}

describe("useJobFlash", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("flashes the result on a running->done transition, then settles to the last-run line", () => {
    vi.useFakeTimers();
    const result = { ok: true, text: "Ratings refreshed" };
    const { container, rerender } = render(<Harness running={true} result={result} />);
    // Mid-run: the spinner, not the confirmation.
    expect(container.querySelector(".spin")).not.toBeNull();

    // The job finishes: the confirmation chip appears in the same slot.
    rerender(<Harness running={false} result={result} />);
    expect(container.querySelector(".flash-chip")?.textContent).toContain("Ratings refreshed");
    expect(container.querySelector(".jobrow-last.is-flash")).not.toBeNull();

    // A few seconds later it settles back to the resting last-run line.
    act(() => {
      vi.advanceTimersByTime(4300);
    });
    expect(container.querySelector(".flash-chip")).toBeNull();
    expect(container.querySelector(".jobrow-last")?.textContent).toContain("Last run");
  });

  it("shows a red chip for a failed manual run", () => {
    vi.useFakeTimers();
    const { container, rerender } = render(
      <Harness running={true} result={{ ok: false, text: "Couldn't refresh lists" }} />,
    );
    rerender(<Harness running={false} result={{ ok: false, text: "Couldn't refresh lists" }} />);
    expect(container.querySelector(".jobrow-last.is-flash-fail")?.textContent).toContain(
      "Couldn't refresh lists",
    );
  });

  it("does not flash on first render of a job that is already idle", () => {
    const { container } = render(
      <Harness running={false} result={{ ok: true, text: "Ratings refreshed" }} />,
    );
    expect(container.querySelector(".flash-chip")).toBeNull();
    expect(container.querySelector(".jobrow-last")?.textContent).toContain("Last run");
  });

  it("clears a still-showing flash the instant a new run starts, instead of hiding the spinner behind it", () => {
    vi.useFakeTimers();
    const first = { ok: true, text: "Ratings refreshed" };
    const { container, rerender } = render(<Harness running={true} result={first} />);
    rerender(<Harness running={false} result={first} />);
    expect(container.querySelector(".flash-chip")?.textContent).toContain("Ratings refreshed");

    // A quick re-click restarts the job well inside the flash window.
    act(() => {
      vi.advanceTimersByTime(500);
    });
    const second = { ok: true, text: "Ratings refreshed" };
    rerender(<Harness running={true} result={second} />);

    // The spinner shows now -- the previous run's chip is not left covering it.
    expect(container.querySelector(".flash-chip")).toBeNull();
    expect(container.querySelector(".spin")).not.toBeNull();

    // The old timer must not fire a stale flash later either.
    act(() => {
      vi.advanceTimersByTime(4300);
    });
    expect(container.querySelector(".spin")).not.toBeNull();
  });
});
