// SPDX-License-Identifier: AGPL-3.0-or-later
//
// What the simulator's wait says, and to whom.
//
// A rescan is driven by a one-second poll rather than by the operator, so the panel changes
// under them: the heading and the button give way to a paragraph and a progress bar. The bar
// announces nothing on its own, so the sentence said when the scan starts is the whole signal
// -- and it was one string for two different pieces of news (#177).
import { render, screen } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import type { Simulation } from "../api";
import { expectNoA11yViolations } from "../test/a11y";
import { Outcome, RESCAN_HEADING, RESCAN_QUEUED_LEAD, StaleNotice } from "./PolicySimulator";

function renderNotice(props: Partial<Parameters<typeof StaleNotice>[0]> = {}) {
  return render(
    <StaleNotice
      scanning
      followupQueued={false}
      starting={false}
      startError={null}
      onScan={() => {}}
      percent={40}
      detail="Scoring"
      staleKind="gathers_differently"
      staleReason="This policy doesn't match the last scan. Scan to apply it."
      {...props}
    />,
  );
}

describe("the wait the simulator shows while a rescan runs", () => {
  it("names itself the same way on the heading and on the progress bar", () => {
    // Both read from one declaration, which is also what the announcement uses. Three copies of
    // one sentence is what this replaced, and the third was the one nobody had noticed (rule
    // 144).
    renderNotice();

    expect(screen.getByRole("heading", { name: RESCAN_HEADING })).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: RESCAN_HEADING })).toBeInTheDocument();
  });

  it("has no accessibility violations in either of its two states", async () => {
    // Both, because this notice is really two screens sharing a container: a waiting state with
    // a progress bar and no control, and a resting one whose only content is the button that
    // starts the scan.
    const waiting = renderNotice();
    await expectNoA11yViolations(waiting.container);
    waiting.unmount();

    const resting = renderNotice({ scanning: false });
    await expectNoA11yViolations(resting.container);
  });

  it("says the changes are in a SECOND scan when one was already running", () => {
    // The bar in front of the operator then belongs to a scan that started before they saved,
    // so it is scoring the old policy. This is the one fact about the wait that is not visible
    // from the bar itself.
    renderNotice({ followupQueued: true });

    expect(screen.getByText(new RegExp(RESCAN_QUEUED_LEAD.slice(0, 40)))).toBeInTheDocument();
  });

  it("does not claim a second scan when this one carries the changes", () => {
    // The other direction, so the branch cannot collapse into always warning about a follow-up
    // (rule 118): a test that passes for both states pins neither.
    renderNotice({ followupQueued: false });

    expect(screen.queryByText(new RegExp(RESCAN_QUEUED_LEAD.slice(0, 40)))).toBeNull();
    expect(screen.getByText(/scoring your library under the new policy/i)).toBeInTheDocument();
  });
});

describe("what the panel says when it will not answer", () => {
  // Nine season controls, the keep tags and the popularity window used to share one
  // paragraph naming every cause at once, so the sentence could only be right about one of
  // them (#495). Each refusal now carries its own remedy, and the operator has to be able to
  // tell them apart at a glance -- which is the whole reason the server sends a typed kind
  // rather than only a sentence.
  const cases = [
    ["gathers_differently", "Needs a fresh scan"],
    ["seasons_not_recorded", "Your season rules need a fresh scan"],
    // Names the control, not a cause: the episode map is also unread after a scan that ran
    // WITH the hold on and got no answer from Sonarr, so "turning that on" was false for
    // that operator (`tests/test_scan_pipeline.py` drives both producers).
    ["in_progress_not_read", "Your partway-through rule needs a fresh scan"],
  ] as const;

  it("gives each refusal its own heading", () => {
    const seen = new Set<string>();
    for (const [kind, heading] of cases) {
      const view = renderNotice({ scanning: false, staleKind: kind });
      expect(screen.getByRole("heading", { name: heading })).toBeInTheDocument();
      seen.add(heading);
      view.unmount();
    }
    // Three distinct headings, not one string reached three ways: a lookup that fell back
    // to the general heading for every kind would satisfy the assertions above one at a
    // time and tell the operator to fix the wrong thing.
    expect(seen.size).toBe(cases.length);
  });

  it("renders the server's sentence rather than a copy of its own", () => {
    // The reason lives in api/routes.py alone. It used to live in both, and the copy the
    // operator actually read was the one in this file -- so the sentence that was reviewed
    // and the sentence that shipped were different strings (rule 144).
    renderNotice({
      scanning: false,
      staleKind: "seasons_not_recorded",
      staleReason: "A sentence only the server could have written.",
    });

    expect(screen.getByText("A sentence only the server could have written.")).toBeInTheDocument();
  });

  it("still says something when the server sends a kind this build does not know", () => {
    // Rule 66: an unknown id falls back, it never guesses. An older browser against a newer
    // server gets the general heading and the server's own sentence, which is always sent.
    renderNotice({
      scanning: false,
      staleKind: "a_refusal_from_the_future" as never,
      staleReason: "Something changed that this scan cannot answer for.",
    });

    expect(screen.getByRole("heading", { name: "Needs a fresh scan" })).toBeInTheDocument();
    expect(
      screen.getByText("Something changed that this scan cannot answer for."),
    ).toBeInTheDocument();
  });
});

// Rule 144, as a guard rather than a comment asking the next author to remember. The panel shows
// these sentences and `PolicyEditor` says them, from two different files -- which is exactly the
// arrangement where a reworded heading leaves the spoken copy behind, and each file's own tests
// stay green because neither can see the other one's string.
//
// Bounded, per rule 147: this reads the sentence however it is spelled around it, but only while
// it is spelled out at all. An editor composing it from pieces would pass, which is why the
// first assertion anchors the declaration rather than letting the walk quietly empty.
describe("who is allowed to write the rescan sentences", () => {
  const read = (name: string) =>
    readFileSync(join(dirname(fileURLToPath(import.meta.url)), name), "utf8");

  it("is this panel, and not the editor that announces them", () => {
    const panel = read("PolicySimulator.tsx");
    expect(panel).toContain(`export const RESCAN_HEADING = "${RESCAN_HEADING}"`);
    expect(panel).toContain(`"${RESCAN_QUEUED_LEAD}"`);

    const editor = read("PolicyEditor.tsx");
    for (const [name, sentence] of [
      ["RESCAN_HEADING", RESCAN_HEADING],
      ["RESCAN_QUEUED_LEAD", RESCAN_QUEUED_LEAD],
    ] as const) {
      expect(
        editor.includes(sentence),
        `PolicyEditor.tsx writes out the ${name} sentence instead of importing it. The panel ` +
          `shows that text and the editor speaks it, so a reword in one leaves the other saying ` +
          `the old thing (#177, rules 72 and 144).`,
      ).toBe(false);
    }
    expect(editor).toContain("RESCAN_QUEUED_LEAD");
  });
});

// What the panel says about an edit that legitimately moves nothing (#488).
//
// A keep rule redraws the histogram while the two headline numbers cannot move, and an operator
// read the frozen outcome as a broken preview. The delta rows were correct throughout, which is
// the trap: they count threshold crossings, and the two rows beside them are absolute totals
// with no before to read them against, so a protection edit that moves a title from spared to
// not judged leaves every number on the panel holding still.
describe("the outcome panel on an edit that changes no title", () => {
  const BASE: Simulation = {
    exact: true,
    stale_kind: null,
    stale_reason: null,
    condemned: 412,
    protected: 388,
    abstained: 2669,
    reclaimable_bytes: 2_090_000_000_000,
    unknown_size_items: 0,
    newly_condemned: 0,
    no_longer_condemned: 0,
    changed_titles: 0,
    histogram: [180, 402, 611, 688, 590, 430, 292, 168, 78, 30],
    examples_newly_condemned: [],
    protected_by: [],
  };

  const INERT = "Your changes leave every title as it is.";

  function renderOutcome(sim: Partial<Simulation>, edited: boolean) {
    return render(
      <Outcome simulation={{ ...BASE, ...sim }} threshold={62} pace={null} edited={edited} />,
    );
  }

  /** The count beside the summary row, read through the row rather than by position. */
  function changed(): string {
    const row = screen.getByText("Titles that change").parentElement;
    return row?.querySelector("dd")?.textContent ?? "";
  }

  it("says so, in place of a comparison between two identical numbers", () => {
    renderOutcome({ changed_titles: 0 }, true);

    expect(screen.getByText(INERT)).toBeInTheDocument();
    // The line it replaces, which is the one that read as broken: "Your saved policy flags
    // 412. This draft flags 412." is a sentence built to contrast two numbers that are equal.
    expect(screen.queryByText(/This draft flags/)).not.toBeInTheDocument();
  });

  it("stays quiet until something has actually been edited", () => {
    // The panel simulates on mount, before anything is touched, so the sentence keyed on the
    // count alone would open by telling the operator their untouched policy does nothing.
    renderOutcome({ changed_titles: 0 }, false);

    expect(screen.queryByText(INERT)).not.toBeInTheDocument();
    expect(screen.getByText(/This draft flags/)).toBeInTheDocument();
  });

  it("keeps the comparison when titles do move", () => {
    renderOutcome({ changed_titles: 65 }, true);

    expect(screen.queryByText(INERT)).not.toBeInTheDocument();
    expect(screen.getByText(/This draft flags/)).toBeInTheDocument();
  });

  it("counts the moves both deltas are blind to", () => {
    // The case from #488's thread: a protection edit takes 65 titles from spared to not judged.
    // Nothing crosses the threshold, so both deltas are correctly zero and both headline numbers
    // correctly hold. Without the summary row this screen is identical to the one above where
    // nothing happened at all -- two outcomes, one picture, on the surface whose job is to show
    // blast radius before saving.
    renderOutcome(
      {
        changed_titles: 65,
        newly_condemned: 0,
        no_longer_condemned: 0,
        protected: 323,
        abstained: 2734,
      },
      true,
    );

    expect(changed()).toBe("65");
    expect(screen.getByText("+0")).toBeInTheDocument();
    expect(screen.getByText("−0")).toBeInTheDocument();
  });

  it("has no accessibility violations", async () => {
    const { container } = renderOutcome({ changed_titles: 0 }, true);
    await expectNoA11yViolations(container);
  });
});
